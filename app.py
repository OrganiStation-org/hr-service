import os
import urllib.request
import json
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME     = os.getenv("DB_NAME", "organistation_hr")
PORT        = int(os.getenv("PORT", "8002"))
HOST        = os.getenv("HOST", "0.0.0.0")
INTERNAL_SERVICE_SECRET = os.getenv("INTERNAL_SERVICE_SECRET", "organistation_internal_secret")

client: AsyncIOMotorClient = None
db = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, db
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client[DB_NAME]
    await db.employees.create_index("email", unique=True, sparse=True)
    await db.leave_requests.create_index("employee_id")
    await db.jobs.create_index("title")
    print(f"[HR Service] Connected to MongoDB: {DB_NAME}")
    yield
    client.close()

app = FastAPI(title="OrganiStation – HR Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def oid(doc):
    doc["id"]  = str(doc["_id"])
    doc["_id"] = str(doc["_id"])
    return doc

async def send_notification(email: str, title: str, message: str, auth_header: str):
    if not auth_header:
        print("[HR Service] No auth header provided; skipping notification")
        return
    
    url = "http://notification:8007/notifications/send-email"
    payload = {
        "email": email,
        "title": title,
        "message": message
    }
    data = json.dumps(payload).encode("utf-8")
    
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": auth_header
        },
        method="POST"
    )
    try:
        loop = asyncio.get_event_loop()
        def do_request():
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.read()
        await loop.run_in_executor(None, do_request)
        print(f"[HR Service] Sent notification email to {email}")
    except Exception as e:
        print(f"[HR Service] Failed to send notification to {email}: {e}")

# ── Schemas ────────────────────────────────────────────────────────────────────

class Employee(BaseModel):
    first_name:  str
    last_name:   str
    email:       Optional[str] = None
    department:  str = "Engineering"
    position:    Optional[str] = None
    phone:       Optional[str] = None
    hire_date:   Optional[str] = None
    salary_lpa:  float = 0.0
    monthly_take_home: float = 0.0
    status:      str = "active"
    # Leave balances
    annual_total:  int = 20
    annual_used:   int = 0
    sick_total:    int = 10
    sick_used:     int = 0
    wfh_total:     int = 12
    wfh_used:      int = 0

class LeaveRequest(BaseModel):
    employee_id: str
    type:        str = "annual"          # annual | sick | unpaid
    start_date:  str
    end_date:    str
    reason:      Optional[str] = None
    status:      str = "pending"

class LeaveUpdate(BaseModel):
    status: str   # approved | rejected

class Job(BaseModel):
    title:       str
    department:  str
    description: Optional[str] = None
    type:        str = "full_time"
    status:      str = "open"
    posted_date: Optional[str] = None

class Attendance(BaseModel):
    employee_id: str
    date:        str
    check_in:    Optional[str] = None
    check_out:   Optional[str] = None
    status:      str = "present"

class PurgeUserRequest(BaseModel):
    email:      str
    first_name: Optional[str] = None
    last_name:  Optional[str] = None

def _verify_internal(x_internal_secret: Optional[str]):
    if x_internal_secret != INTERNAL_SERVICE_SECRET:
        raise HTTPException(403, "Forbidden")

async def _delete_employee_records(eid: str):
    await db.attendance.delete_many({"employee_id": eid})
    await db.leave_requests.delete_many({"employee_id": eid})
    result = await db.employees.delete_one({"_id": ObjectId(eid)})
    return result.deleted_count

# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/")
@app.get("/health")
@app.get("/api/health")
async def health():
    return {"status": "healthy", "service": "hr-service"}

# ── Employees ──────────────────────────────────────────────────────────────────

@app.get("/api/employees")
async def list_employees():
    cur = db.employees.find()
    return [oid(e) async for e in cur]

@app.get("/api/employees/{eid}")
async def get_employee(eid: str):
    e = await db.employees.find_one({"_id": ObjectId(eid)})
    if not e: raise HTTPException(404, "Employee not found")
    return oid(e)

@app.post("/api/employees", status_code=201)
async def create_employee(emp: Employee):
    doc = emp.model_dump()
    doc["created_at"] = doc["updated_at"] = datetime.utcnow()
    r = await db.employees.insert_one(doc)
    doc["id"] = doc["_id"] = str(r.inserted_id)
    return doc

@app.put("/api/employees/{eid}")
async def update_employee(eid: str, emp: Employee):
    data = {k: v for k, v in emp.model_dump().items() if v is not None}
    data["updated_at"] = datetime.utcnow()
    await db.employees.update_one({"_id": ObjectId(eid)}, {"$set": data})
    e = await db.employees.find_one({"_id": ObjectId(eid)})
    if not e: raise HTTPException(404, "Employee not found")
    return oid(e)

@app.delete("/api/employees/{eid}")
async def delete_employee(eid: str):
    employee = await db.employees.find_one({"_id": ObjectId(eid)})
    if not employee:
        raise HTTPException(404, "Employee not found")
    await _delete_employee_records(eid)
    return {"message": "Employee and related HR data deleted"}

@app.post("/api/internal/purge-user")
async def purge_user(
    body: PurgeUserRequest,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
):
    _verify_internal(x_internal_secret)
    employee = await db.employees.find_one({"email": body.email})
    if not employee:
        return {"employees_deleted": 0, "attendance_deleted": 0, "leaves_deleted": 0}

    eid = str(employee["_id"])
    attendance_deleted = (await db.attendance.delete_many({"employee_id": eid})).deleted_count
    leaves_deleted = (await db.leave_requests.delete_many({"employee_id": eid})).deleted_count
    employees_deleted = (await db.employees.delete_one({"_id": employee["_id"]})).deleted_count
    return {
        "employees_deleted": employees_deleted,
        "attendance_deleted": attendance_deleted,
        "leaves_deleted": leaves_deleted,
    }

# ── Attendance ─────────────────────────────────────────────────────────────────

@app.get("/api/employees/{eid}/attendance")
async def get_attendance(eid: str):
    cur = db.attendance.find({"employee_id": eid})
    return [oid(a) async for a in cur]

@app.post("/api/attendance", status_code=201)
async def log_attendance(a: Attendance):
    doc = a.model_dump()
    doc["created_at"] = datetime.utcnow()
    r = await db.attendance.insert_one(doc)
    doc["id"] = doc["_id"] = str(r.inserted_id)
    return doc

# ── Leave Requests ─────────────────────────────────────────────────────────────

@app.get("/api/leaves")
async def list_leaves(employee_id: Optional[str] = None):
    query = {}
    if employee_id:
        query["employee_id"] = employee_id
    cur = db.leave_requests.find(query).sort("created_at", -1)
    return [oid(l) async for l in cur]

@app.post("/api/leaves", status_code=201)
async def create_leave(req: LeaveRequest, authorization: Optional[str] = Header(None)):
    doc = req.model_dump()
    doc["created_at"] = datetime.utcnow()
    r = await db.leave_requests.insert_one(doc)
    doc["id"] = doc["_id"] = str(r.inserted_id)

    # Trigger notification
    try:
        emp = await db.employees.find_one({"_id": ObjectId(req.employee_id)})
        if emp:
            emp_name = f"{emp.get('first_name', '')} {emp.get('last_name', '')}"
            # Find HR team members
            hr_managers = await db.employees.find({"department": "HR"}).to_list(length=10)
            hr_emails = [e["email"] for e in hr_managers if e.get("email")]
            if not hr_emails:
                hr_emails = ["hr@organistation.com"]
            
            for hr_email in hr_emails:
                await send_notification(
                    email=hr_email,
                    title=f"New Leave Request: {emp_name}",
                    message=f"{emp_name} has requested {req.type.capitalize()} leave from {req.start_date} to {req.end_date}. Reason: {req.reason or 'None'}",
                    auth_header=authorization
                )
    except Exception as ex:
        print(f"[HR Service] Error preparing apply leave notification: {ex}")

    return doc

@app.put("/api/leaves/{lid}")
async def update_leave(lid: str, upd: LeaveUpdate, authorization: Optional[str] = Header(None)):
    # Get current leave info
    leave = await db.leave_requests.find_one({"_id": ObjectId(lid)})
    if not leave: raise HTTPException(404, "Leave request not found")
    
    old_status = leave.get("status")
    new_status = upd.status
    
    # Update status
    await db.leave_requests.update_one(
        {"_id": ObjectId(lid)}, 
        {"$set": {"status": new_status, "updated_at": datetime.utcnow()}}
    )
    
    # If newly approved, update employee balance
    if new_status == "approved" and old_status != "approved":
        eid = leave.get("employee_id")
        ltype = leave.get("type", "annual")
        
        # Calculate days (very simple version: just count days between start and end inclusive)
        try:
            fmt = "%Y-%m-%d"
            start = datetime.strptime(leave["start_date"], fmt)
            end = datetime.strptime(leave["end_date"], fmt)
            days = (end - start).days + 1
            if days < 0: days = 0
            
            field_name = f"{ltype}_used"
            # We don't have separate unpaid balance, so we only update for annual, sick, wfh
            if field_name in ["annual_used", "sick_used", "wfh_used"]:
                await db.employees.update_one(
                    {"_id": ObjectId(eid)},
                    {"$inc": {field_name: days}}
                )
        except Exception as e:
            print(f"Failed to update balance for leave {lid}: {e}")

    # Trigger notification
    try:
        emp = await db.employees.find_one({"_id": ObjectId(leave["employee_id"])})
        if emp and emp.get("email"):
            await send_notification(
                email=emp["email"],
                title=f"Leave Request {new_status.capitalize()}",
                message=f"Your request for {leave.get('type', 'annual').capitalize()} leave from {leave.get('start_date')} to {leave.get('end_date')} has been {new_status}.",
                auth_header=authorization
            )
    except Exception as ex:
        print(f"[HR Service] Error preparing status update notification: {ex}")

    doc = await db.leave_requests.find_one({"_id": ObjectId(lid)})
    return oid(doc)

# ── Jobs / Recruitment ─────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def list_jobs():
    cur = db.jobs.find()
    return [oid(j) async for j in cur]

@app.post("/api/jobs", status_code=201)
async def create_job(job: Job):
    doc = job.model_dump()
    doc["created_at"] = datetime.utcnow()
    r = await db.jobs.insert_one(doc)
    doc["id"] = doc["_id"] = str(r.inserted_id)
    return doc

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=True)

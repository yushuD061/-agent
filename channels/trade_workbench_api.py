"""Loopback-only API router for the internal trade workbench."""
from __future__ import annotations
import hashlib, ipaddress, secrets
from pathlib import Path
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from agent.business.trade_workbench_repository import TradeWorkbenchError, create_trade_workbench_repository
from agent.business.trade_workbench_service import TradeStageDrainer, TradeWorkbenchService


def _loopback(value: str | None) -> bool:
    if value in {"testclient", "testserver", "localhost"}: return True
    try: return ipaddress.ip_address(value or "").is_loopback
    except ValueError: return False


def build_trade_workbench_router(service: TradeWorkbenchService | None = None, *,
                                 read_only: bool = False) -> APIRouter:
    router=APIRouter(prefix="/api/trade",tags=["trade-workbench"])
    resolved=service
    csrf_tokens:set[str]=set()

    def svc()->TradeWorkbenchService:
        nonlocal resolved
        if resolved is None: resolved=TradeWorkbenchService(create_trade_workbench_repository())
        return resolved

    def guard(request:Request,*,write:bool=False)->None:
        host=(request.url.hostname or "").lower(); client=request.client.host if request.client else ""
        if not (_loopback(host) and _loopback(client)): raise HTTPException(403,"trade_loopback_required")
        origin=request.headers.get("origin")
        if origin:
            parsed=urlparse(origin)
            request_port=request.url.port or (443 if request.url.scheme=="https" else 80)
            origin_port=parsed.port or (443 if parsed.scheme=="https" else 80)
            if (parsed.scheme != request.url.scheme or not _loopback(parsed.hostname)
                    or parsed.hostname != host or origin_port != request_port):
                raise HTTPException(403,"trade_origin_forbidden")
        if write:
            if read_only:
                raise HTTPException(410,"legacy_trade_workbench_read_only")
            token=request.headers.get("x-csrf-token",""); cookie=request.cookies.get("trade_csrf","")
            if not token or not cookie or token not in csrf_tokens or not secrets.compare_digest(token,cookie): raise HTTPException(403,"trade_csrf_invalid")
            if not request.headers.get("idempotency-key"): raise HTTPException(400,"trade_idempotency_key_required")

    def fail(exc:Exception):
        if isinstance(exc,TradeWorkbenchError): raise HTTPException(exc.status_code,exc.code)
        raise HTTPException(500,"trade_workbench_internal_error") from exc

    def idempotent(request:Request,scope:str,payload:dict,action):
        return svc().repository.idempotent(scope,request.headers["idempotency-key"],payload,action)

    @router.get("/csrf")
    async def csrf(request:Request,response:Response):
        guard(request); token=secrets.token_urlsafe(32);csrf_tokens.add(token)
        if len(csrf_tokens)>1000: csrf_tokens.pop()
        response.set_cookie("trade_csrf",token,httponly=True,samesite="strict",secure=False,path="/api/trade")
        return {"csrf_token":token}

    @router.get("/input-status")
    async def input_status(request:Request): guard(request); return svc().input_status()

    @router.get("/campaigns")
    async def campaigns(request:Request): guard(request); return {"items":svc().repository.list_campaigns()}

    @router.post("/campaigns",status_code=201)
    async def create_campaign(request:Request):
        guard(request,write=True); payload=await request.json()
        try:return idempotent(request,"campaign.create",payload,lambda:svc().create_campaign(str(payload.get("name") or "")))
        except Exception as exc: fail(exc)

    @router.get("/campaigns/{campaign_id}")
    async def campaign(campaign_id:str,request:Request,response:Response):
        guard(request)
        try:
            result=svc().repository.get_campaign(campaign_id);response.headers["ETag"]=result["etag"];return result
        except Exception as exc:fail(exc)

    @router.post("/campaigns/{campaign_id}/inputs")
    async def save_input(campaign_id:str,request:Request):
        guard(request,write=True);payload=await request.json()
        try:
            return idempotent(request,f"campaign:{campaign_id}:input",payload,lambda:svc().repository.save_input(campaign_id,str(payload.get("input_type") or "generic"),payload.get("payload") or {},source_name=payload.get("source_name")))
        except Exception as exc:fail(exc)

    @router.post("/campaigns/{campaign_id}/imports/{input_type}")
    async def import_input(campaign_id:str,input_type:str,request:Request):
        guard(request,write=True);filename=request.headers.get("x-filename","").strip()
        if not filename: raise HTTPException(400,"trade_import_filename_required")
        raw=await request.body()
        payload={"input_type":input_type,"filename":filename,"sha256":hashlib.sha256(raw).hexdigest()}
        try:return idempotent(request,f"campaign:{campaign_id}:import:{input_type}",payload,
          lambda:svc().import_input_bytes(campaign_id,input_type,filename,raw))
        except Exception as exc:fail(exc)

    @router.post("/campaigns/{campaign_id}/stages/{stage}/run")
    async def run_stage(campaign_id:str,stage:str,request:Request):
        guard(request,write=True);payload=await request.json()
        def command():
            repository=svc().repository
            job=repository.enqueue_job(campaign_id,stage,payload)
            outcomes=TradeStageDrainer(repository,svc()).drain_once(20)
            result=repository.latest_stage_result(campaign_id,stage)
            return {"job_id":job.job_id,"job_status":repository.enqueue_job(campaign_id,stage,payload).status,
                    "drain":outcomes,"stage_result":result.__dict__ if result else None}
        try:return idempotent(request,f"campaign:{campaign_id}:stage:{stage}",payload,command)
        except Exception as exc:fail(exc)

    @router.post("/campaigns/{campaign_id}/stages/{stage}/retry")
    async def retry_stage(campaign_id:str,stage:str,request:Request):
        guard(request,write=True);payload=await request.json();key=request.headers["idempotency-key"]
        def command():
            repository=svc().repository
            job=repository.enqueue_job(campaign_id,stage,payload,business_key=f"retry:{campaign_id}:{stage}:{key}")
            outcomes=TradeStageDrainer(repository,svc()).drain_once(20)
            result=repository.latest_stage_result(campaign_id,stage)
            return {"job_id":job.job_id,"drain":outcomes,"stage_result":result.__dict__ if result else None}
        try:return idempotent(request,f"campaign:{campaign_id}:stage:{stage}:retry",payload,command)
        except Exception as exc:fail(exc)

    @router.post("/campaigns/{campaign_id}/pause")
    async def pause(campaign_id:str,request:Request):
        guard(request,write=True);etag=request.headers.get("if-match")
        try:return idempotent(request,f"campaign:{campaign_id}:pause",{"etag":etag},lambda:svc().repository.pause(campaign_id,expected_etag=etag))
        except Exception as exc:fail(exc)

    @router.post("/campaigns/{campaign_id}/resume")
    async def resume(campaign_id:str,request:Request):
        guard(request,write=True);etag=request.headers.get("if-match")
        try:return idempotent(request,f"campaign:{campaign_id}:resume",{"etag":etag},lambda:svc().repository.resume(campaign_id,expected_etag=etag))
        except Exception as exc:fail(exc)

    @router.post("/email-drafts/{draft_id}/approve")
    async def approve_email(draft_id:str,request:Request):
        guard(request,write=True);payload=await request.json()
        try:return idempotent(request,f"email:{draft_id}:approve",payload,lambda:svc().approve_outreach(draft_id,expected_hash=str(payload.get("content_hash") or "")))
        except Exception as exc:fail(exc)

    @router.post("/email-drafts/{draft_id}/queue")
    async def queue_email(draft_id:str,request:Request):
        guard(request,write=True);payload=await request.json()
        try:return idempotent(request,f"email:{draft_id}:queue",payload,lambda:svc().queue_outreach(draft_id,account_id=str(payload.get("account_id") or ""),recipient=str(payload.get("recipient") or ""),expected_hash=str(payload.get("content_hash") or "")))
        except Exception as exc:fail(exc)

    @router.post("/quote-drafts/{quote_id}/approve")
    async def approve_quote(quote_id:str,request:Request):
        guard(request,write=True);payload=await request.json()
        try:return idempotent(request,f"quote:{quote_id}:approve",payload,lambda:svc().approve_quote(quote_id,expected_hash=str(payload.get("content_hash") or "")))
        except Exception as exc:fail(exc)

    @router.get("/quote-drafts/{quote_id}/artifacts/{kind}")
    async def quote_artifact(quote_id:str,kind:str,request:Request):
        guard(request)
        if kind not in {"json","html","xlsx"}: raise HTTPException(404,"trade_artifact_not_found")
        repository=svc().repository
        stored_kind="excel" if kind=="xlsx" else kind
        try: row=repository.get_artifact("quote",quote_id,stored_kind)
        except Exception as exc:fail(exc)
        if not row: raise HTTPException(404,"trade_artifact_not_found")
        root=svc().artifact_root.resolve(); path=Path(row["storage_path"]).resolve()
        if not path.is_file() or root not in path.parents: raise HTTPException(404,"trade_artifact_not_found")
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row["artifact_sha256"]:
            raise HTTPException(409,"trade_artifact_hash_mismatch")
        media={"json":"application/json","html":"text/html","xlsx":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[kind]
        return FileResponse(path,media_type=media,filename=row["file_name"])

    return router

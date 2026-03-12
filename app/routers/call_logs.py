from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import uuid
import json
from app.core.logger import logger

from app.api.deps import get_db, require_tenant
from app.models.user import User
from app.models.call_session import CallSession
from app.models.call_log import CallLog
from app.models.agent import Agent
from app.schemas.call_log import (
    CallLogResponse,
    CallLogFilters,
    CallLogStats,
    CallLogList,
)
from app.schemas.call_session import CallLogAnalysisEmailRequest
from app.services.call_log_service import CallLogService
from app.services.email_service import email_service
from app.services.openai_service import openai_service
from app.services.model_service import model_service
from app.core.security import decrypt_api_key
from app.services.transcript_service import transcript_service
from app.utils.response import create_success_response
from app.schemas.base import SuccessResponse

router = APIRouter()

@router.get("/call-logs", response_model=CallLogList)
async def get_call_logs(
    # Pagination
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=100, description="Items per page"),
    
    # Filters
    call_type: Optional[str] = Query(None, description="Filter by call type (inbound, outbound, web)"),
    success_evaluation: Optional[str] = Query(None, description="Filter by success (success, fail, null)"),
    agent_id: Optional[uuid.UUID] = Query(None, description="Filter by agent ID"),
    date_from: Optional[datetime] = Query(None, description="Filter from date"),
    date_to: Optional[datetime] = Query(None, description="Filter to date"),
    transferred: Optional[bool] = Query(None, description="Filter by transferred calls"),
    ended_reason: Optional[str] = Query(None, description="Filter by ended reason"),
    
    # User and database
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Get call logs with filtering and pagination
    Comprehensive call logging system for monitoring all call activities
    """
    try:
        logger.info(f"📊 GETTING CALL LOGS")
        logger.debug(f"👤 User: {user.email}")
        logger.debug(f"🏢 Tenant: {user.current_tenant_id}")
        logger.debug(f"📄 Page: {page}, Per Page: {per_page}")
        logger.debug(f"🔍 Filters: type={call_type}, success={success_evaluation}, agent={agent_id}")
        
        # Create filters object
        filters = CallLogFilters(
            call_type=call_type,
            success_evaluation=success_evaluation,
            agent_id=agent_id,
            date_from=date_from,
            date_to=date_to,
            transferred=transferred,
            ended_reason=ended_reason
        )
        
        # Get call logs using service
        call_logs_result = CallLogService.get_call_logs(
            db=db,
            tenant_id=user.current_tenant_id,
            filters=filters,
            page=page,
            per_page=per_page
        )
        
        logger.info(f"✅ Found {call_logs_result['total']} call logs")
        logger.debug(f"📊 Stats: {call_logs_result['stats']}")
        
        return create_success_response(
            call_logs_result,
            f"Retrieved {len(call_logs_result['logs'])} call logs successfully"
        )
        
    except Exception as e:
        logger.error(f"❌ Error getting call logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get call logs: {str(e)}")


@router.get("/call-logs/{call_log_id}", response_model=CallLogResponse)
async def get_call_log_detail(
    call_log_id: uuid.UUID,
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Get detailed information about a specific call log
    """
    try:
        logger.info(f"📋 GETTING CALL LOG DETAIL")
        logger.debug(f"🆔 Call Log ID: {call_log_id}")
        logger.debug(f"👤 User: {user.email}")
        logger.debug(f"🏢 Tenant: {user.current_tenant_id}")
        
        # Get call log detail
        call_log = CallLogService.get_call_log_by_id(
            db=db,
            call_log_id=call_log_id,
            tenant_id=user.current_tenant_id
        )
        
        if not call_log:
            raise HTTPException(status_code=404, detail="Call log not found")
        
        logger.info(f"✅ Found call log: {call_log.call_id}")
        logger.debug(f"📞 Phone: {call_log.customer_phone_number}")
        logger.debug(f"⏱️ Duration: {call_log.duration} seconds")
        logger.debug(f"📊 Status: {call_log.success_evaluation}")
        
        return create_success_response(
            call_log,
            "Call log detail retrieved successfully"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error getting call log detail: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get call log detail: {str(e)}")


@router.get("/call-logs/stats", response_model=CallLogStats)
async def get_call_logs_stats(
    # Date range
    date_from: Optional[datetime] = Query(None, description="Stats from date"),
    date_to: Optional[datetime] = Query(None, description="Stats to date"),
    
    # User and database
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Get call logs statistics and analytics
    """
    try:
        logger.info(f"📈 GETTING CALL LOGS STATS")
        logger.debug(f"👤 User: {user.email}")
        logger.debug(f"🏢 Tenant: {user.current_tenant_id}")
        logger.debug(f"📅 Date Range: {date_from} to {date_to}")
        
        # Get call logs statistics
        stats = CallLogService.get_call_logs_stats(
            db=db,
            tenant_id=user.current_tenant_id,
            date_from=date_from,
            date_to=date_to
        )
        
        logger.info(f"📊 Total Calls: {stats.total_calls}")
        logger.debug(f"✅ Successful: {stats.successful_calls}")
        logger.debug(f"❌ Failed: {stats.failed_calls}")
        logger.debug(f"🔄 Transferred: {stats.transferred_calls}")
        logger.debug(f"💰 Total Cost: ${stats.total_cost}")
        logger.debug(f"⏱️ Avg Duration: {stats.average_duration} seconds")
        
        return create_success_response(
            stats,
            "Call logs statistics retrieved successfully"
        )
        
    except Exception as e:
        logger.error(f"❌ Error getting call logs stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get call logs stats: {str(e)}")


@router.get("/call-logs/agent/{agent_id}")
async def get_agent_call_logs(
    agent_id: uuid.UUID,
    # Pagination
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=100, description="Items per page"),
    
    # Date range
    date_from: Optional[datetime] = Query(None, description="Filter from date"),
    date_to: Optional[datetime] = Query(None, description="Filter to date"),
    
    # User and database
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Get call logs for a specific agent
    """
    try:
        logger.info(f"🤖 GETTING AGENT CALL LOGS")
        logger.debug(f"🆔 Agent ID: {agent_id}")
        logger.debug(f"👤 User: {user.email}")
        logger.debug(f"🏢 Tenant: {user.current_tenant_id}")
        
        # Verify agent belongs to tenant
        agent = db.query(Agent).filter(
            Agent.id == agent_id,
            Agent.tenant_id == user.current_tenant_id
        ).first()
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Create filters for agent
        filters = CallLogFilters(
            agent_id=agent_id,
            date_from=date_from,
            date_to=date_to
        )
        
        # Get agent call logs
        call_logs_result = CallLogService.get_call_logs(
            db=db,
            tenant_id=user.current_tenant_id,
            filters=filters,
            page=page,
            per_page=per_page
        )
        
        logger.info(f"✅ Found {call_logs_result['total']} calls for agent: {agent.name}")
        
        return create_success_response(
            {
                "agent": {
                    "id": agent.id,
                    "name": agent.name,
                    "description": agent.description
                },
                "call_logs": call_logs_result
            },
            f"Retrieved {len(call_logs_result['logs'])} call logs for agent {agent.name}"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error getting agent call logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get agent call logs: {str(e)}")


@router.get("/call-logs/recent")
async def get_recent_call_logs(
    limit: int = Query(10, ge=1, le=50, description="Number of recent calls"),
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Get recent call logs for quick monitoring
    """
    try:
        logger.info(f"🕐 GETTING RECENT CALL LOGS")
        logger.debug(f"👤 User: {user.email}")
        logger.debug(f"🏢 Tenant: {user.current_tenant_id}")
        logger.debug(f"📊 Limit: {limit}")
        
        # Get recent call logs
        recent_logs = CallLogService.get_recent_call_logs(
            db=db,
            tenant_id=user.current_tenant_id,
            limit=limit
        )
        
        logger.info(f"✅ Found {len(recent_logs)} recent call logs")
        
        return create_success_response(
            {
                "recent_logs": recent_logs,
                "count": len(recent_logs)
            },
            f"Retrieved {len(recent_logs)} recent call logs"
        )
        
    except Exception as e:
        logger.error(f"❌ Error getting recent call logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get recent call logs: {str(e)}")


@router.get("/call-logs/export")
async def export_call_logs(
    # Date range
    date_from: Optional[datetime] = Query(None, description="Export from date"),
    date_to: Optional[datetime] = Query(None, description="Export to date"),
    
    # Format
    format: str = Query("json", description="Export format (json, csv)"),
    
    # User and database
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Export call logs in various formats
    """
    try:
        logger.info(f"📤 EXPORTING CALL LOGS")
        logger.debug(f"👤 User: {user.email}")
        logger.debug(f"🏢 Tenant: {user.current_tenant_id}")
        logger.debug(f"📅 Date Range: {date_from} to {date_to}")
        logger.debug(f"📄 Format: {format}")
        
        # Get all call logs for export
        filters = CallLogFilters(
            date_from=date_from,
            date_to=date_to
        )
        
        # Get all call logs (no pagination for export)
        call_logs_result = CallLogService.get_call_logs(
            db=db,
            tenant_id=user.current_tenant_id,
            filters=filters,
            page=1,
            per_page=10000  # Large number to get all
        )
        
        if format.lower() == "csv":
            # Convert to CSV format
            csv_data = CallLogService.export_to_csv(call_logs_result['logs'])
            return create_success_response(
                {"csv_data": csv_data, "count": len(call_logs_result['logs'])},
                f"Exported {len(call_logs_result['logs'])} call logs to CSV"
            )
        else:
            # Return JSON format
            return create_success_response(
                {
                    "call_logs": call_logs_result['logs'],
                    "stats": call_logs_result['stats'],
                    "count": len(call_logs_result['logs'])
                },
                f"Exported {len(call_logs_result['logs'])} call logs to JSON"
            )
        
    except Exception as e:
        logger.error(f"❌ Error exporting call logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to export call logs: {str(e)}")


@router.post("/call-logs/send-email", response_model=SuccessResponse[dict])
async def send_call_analysis_email(
    payload: CallLogAnalysisEmailRequest,
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db),
):
    """
    Send a call-related email based on a call session.

    - If transform_prompt is NOT provided: backend generates an analysis and forwards it as the email body.
    - If transform_prompt IS provided: backend generates an analysis, then uses the prompt to create a custom email.

    Email is sent from the platform's domain sender, with the logged-in user automatically CC'd.
    """
    try:
        # 1) Validate call session belongs to this tenant
        session = db.query(CallSession).filter(
            CallSession.id == payload.call_session_id,
            CallSession.tenant_id == user.current_tenant_id,
        ).first()
        if not session:
            raise HTTPException(status_code=404, detail="Call session not found")

        # 1b) Load agent and its system prompt for additional context
        agent = db.query(Agent).filter(
            Agent.id == session.agent_id,
            Agent.tenant_id == user.current_tenant_id,
        ).first()
        agent_system_prompt = agent.system_prompt or "" if agent else ""

        # 2) Build flat transcript text
        # Prefer detailed TranscriptMessage records (via transcript_service).
        transcript_lines: list[str] = []
        try:
            messages = transcript_service.get_messages_by_session(db, session.id)
            if messages:
                for msg in messages:
                    # Normalize roles for readability
                    role = msg.role.capitalize() if msg.role else "Unknown"
                    transcript_lines.append(f"{role}: {msg.message}")
            else:
                # Fallback to legacy JSON transcript on CallSession if present
                session_transcript = session.call_transcript or []
                for entry in session_transcript:
                    role = entry.get("role", "unknown").capitalize()
                    content = entry.get("content", "")
                    transcript_lines.append(f"{role}: {content}")
        except Exception as e:
            logger.error(f"Failed to load transcript messages for session {session.id}: {e}", exc_info=True)
            # As a final fallback, still try call_transcript
            session_transcript = session.call_transcript or []
            for entry in session_transcript:
                role = entry.get("role", "unknown").capitalize()
                content = entry.get("content", "")
                transcript_lines.append(f"{role}: {content}")

        transcript_text = "\n".join(transcript_lines) if transcript_lines else "No transcript available."

        # 3) Resolve OpenAI model and API key for gpt-4o-mini
        model_name = "gpt-4o-mini"
        api_key: Optional[str] = None
        try:
            model = model_service.get_model_by_name(db, model_name)
            if model and model.api_key:
                try:
                    api_key = decrypt_api_key(model.api_key)
                except Exception as e:
                    logger.error(f"Failed to decrypt API key for model '{model_name}': {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Failed to load model configuration for '{model_name}': {e}", exc_info=True)

        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "OpenAI API key for model 'gpt-4o-mini' is not configured. "
                    "Please set an API key on the model in the database or configure OPENAI_API_KEY."
                ),
            )

        # 4) Always generate an analysis from the transcript (used directly or as context)
        analysis_system = (
            "You are an expert recruiting and call analytics assistant. "
            "You are analyzing a call handled by an AI voice agent.\n\n"
            f"Agent system prompt (how the agent is supposed to behave):\n\"\"\"{agent_system_prompt}\"\"\"\n\n"
            "Use the agent's system prompt to understand whether this is a hiring/interview context and to guide tone, "
            "but ONLY use facts that are clearly present in the transcript or explicitly provided by the user. "
            "Do not invent or guess participant names, dates, times, positions, companies, or any other specific details.\n\n"
            "If the call is an interview or recruiting call (as implied by the agent's system prompt), refer to the human "
            "talking to the agent as the 'candidate' rather than 'customer' or 'user'.\n\n"
            "From the following transcript, produce a concise, bullet-based summary that includes:\n"
            "- Who the caller is (only if clearly stated; otherwise skip this point)\n"
            "- Key topics discussed\n"
            "- Any clear strengths / concerns or sentiment\n"
            "- A realistic recommendation and next steps based only on what is actually said."
        )
        analysis_user = f"""
Call transcript:
\"\"\"{transcript_text}\"\"\"
"""
        analysis_res = openai_service.chat_completion(
            messages=[{"role": "user", "content": analysis_user}],
            system_prompt=analysis_system,
            model_name=model_name,
            temperature=0.3,
            max_tokens=800,
            api_key=api_key,
        )
        analysis_text = analysis_res["content"]

        # 5) Decide final email body:
        # - If no transform_prompt: forward analysis as email body
        # - If transform_prompt provided: let AI create a formatted email
        if not payload.transform_prompt:
            email_body_text = analysis_text
        else:
            email_system = f"""
You are an AI assistant that writes emails based on:
- A call analysis
- The raw call transcript
- The agent's system prompt
- The user's email instruction (prompt)

Agent system prompt (how the agent is supposed to behave):
\"\"\"{agent_system_prompt}\"\"\"

STRICT RULES ABOUT DATA:
- You MUST NOT invent specific details (name, date, time, company, position) if they are not clearly present
  in the analysis, transcript, the agent prompt, or the user's instruction.
- If the candidate's name is NOT clearly known, use a generic greeting like "Hi there," and DO NOT use any placeholder.
- If the position title is NOT clearly known, do NOT add a "Position:" line.
- If the company name is clearly specified in the agent prompt or user instruction, you MAY use it;
  otherwise do NOT invent a company name.
- If interview date/time is NOT clearly provided (in the user instruction, analysis, or transcript),
  do NOT fabricate a date or time. You may refer to "your upcoming interview" in general, but no fake specifics.
- NEVER output placeholder markers like [Candidate's Name], [Insert Date], [Your Company], etc.

STYLE & TTS:
- Follow the user's instruction for tone and language.
- Use short paragraphs and clear punctuation so that the email sounds natural when read aloud by text-to-speech.
- Keep it professional and aligned with the agent's tone (from the agent system prompt).
"""
            email_user = f"""
User instruction (prompt):
\"\"\"{payload.transform_prompt}\"\"\"

Call analysis:
\"\"\"{analysis_text}\"\"\"

Call transcript (for reference):
\"\"\"{transcript_text}\"\"\"
"""
            email_res = openai_service.chat_completion(
                messages=[{"role": "user", "content": email_user}],
                system_prompt=email_system,
                model_name=model_name,
                temperature=0.4,
                max_tokens=600,
                api_key=api_key,
            )
            email_body_text = email_res["content"]

        # 6) Append CC information to the plain text body and wrap as simple HTML
        footer_cc_line = f"\n\n(CC: {user.email})"
        email_body_text = email_body_text + footer_cc_line
        html_body = "<html><body>" + "<br/>".join(email_body_text.splitlines()) + "</body></html>"

        # 7) Subject depending on whether we used a custom prompt
        subject = "Call analysis" if not payload.transform_prompt else "Call follow-up"

        # 8) Send email from domain sender, CC logged-in user
        success = email_service.send_generic_email(
            to_email=payload.target_email,
            subject=subject,
            html_body=html_body,
            cc_emails=[user.email],
        )
        if not success:
            raise HTTPException(status_code=500, detail="Failed to send email")

        return create_success_response(
            {
                "sent": True,
                "target_email": payload.target_email,
                "cc_email": user.email,
                "analysis_used": analysis_text,
                "email_body": email_body_text,
            },
            "Call-related email sent successfully",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error sending call analysis email: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to send call analysis email: {str(e)}")
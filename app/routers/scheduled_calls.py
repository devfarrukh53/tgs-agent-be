"""
Scheduled Calls API endpoints with Monday.com integration (per-user boards).
All tenants of a user share the same board, identified by tenant_id column in items.
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Request, status, Body
from sqlalchemy.orm import Session
from sqlalchemy import and_
from datetime import datetime, timezone
import uuid
from app.api.deps import get_db, require_tenant, get_optional_tenant_user, require_owner
from app.utils.n8n_webhook_verification import verify_n8n_webhook_secret_async
from app.models.user import User
from app.models.agent import Agent
from app.models.call_session import CallSession
from app.schemas.scheduled_call import (
    CSVUploadResponse,
    BoardInfoResponse,
    DeleteBoardItemsResponse,
    SingleCallRequest,
    SingleCallResponse,
    PendingCountResponse,
    PendingCountByCrm,
    JiraBatchAnalysisRequest,
    ScheduleFromCallSessionRequest,
)
from app.schemas.crm_config import CRMConfigResponse, CRMConfigListResponse, CRMConfigListItem
from app.services.scheduled_call_service import ScheduledCallService
from app.services.monday_service import MondayService
from app.services.clickup_service import ClickUpService
from app.services.trello_service import TrelloService
from app.services.jira_service import JiraService
from app.services.crm_config_service import CRMConfigService
from app.services.crm_service_factory import CRMServiceFactory
from app.models.scheduled_call import ScheduledCall
from app.services.transcript_service import transcript_service
from app.services.agent_service import agent_service
from app.services.model_service import ModelService
from app.services.call_session_service import call_session_service
from app.services.phone_number_service import phone_number_service
from app.utils.response import create_success_response
from app.schemas.base import SuccessResponse
from typing import Optional, Dict, Any, List
import re

router = APIRouter()

scheduled_call_service = ScheduledCallService()
model_service = ModelService()
crm_config_service = CRMConfigService()


async def analyze_call_transcript_internal(
    db: Session,
    call_session: CallSession,
    user: User
) -> Optional[Dict[str, Any]]:
    """
    Internal helper function to analyze a call transcript.
    Returns analysis dict or None if analysis fails.
    """
    try:
        # Get transcript messages
        transcript_messages = transcript_service.get_messages_by_session(db, call_session.id)
        
        if not transcript_messages:
            return None
        
        # Format transcript for analysis
        transcript_text = ""
        for msg in transcript_messages:
            role_label = "Agent" if msg.role == "agent" else "Customer"
            transcript_text += f"{role_label}: {msg.message}\n"
        
        # Get agent and model info
        agent = None
        agent_prompt = None
        preferred_model = None
        
        if call_session.agent_id:
            try:
                agent = agent_service.get_agent_by_id(db, call_session.agent_id, call_session.tenant_id)
                if agent:
                    if agent.system_prompt:
                        agent_prompt = agent.system_prompt
                    elif agent.model and agent.model.system_prompt:
                        agent_prompt = agent.model.system_prompt
                    
                    if agent and agent.model:
                        preferred_model = agent.model.model_name
            except Exception:
                pass
        
        # Fallback models
        fallback_models = [
            preferred_model,
            "gemini-2.0-flash",
            "llama-3.3-70b-versatile",
            "gpt-4o-mini"
        ]
        fallback_models = [m for m in fallback_models if m]
        
        # Find available model
        model = None
        for model_name in fallback_models:
            try:
                model = model_service.get_model_by_name(db, model_name)
                if model:
                    break
            except Exception:
                continue
        
        if not model:
            return None
        
        # Get API key
        current_api_key = None
        if model.api_key:
            from app.core.security import decrypt_api_key
            current_api_key = decrypt_api_key(model.api_key)
        
        # Create prompts
        summary_prompt = f"""
        Analyze this call transcript and provide a brief summary in 2-3 sentences.
        
        Call Transcript:
        {transcript_text}
        
        Provide only:
        - Brief call overview
        - Main topic/issue
        - Outcome/resolution
        
        Keep it concise and to the point.
        """
        
        sentiment_prompt = f"""
        Analyze the sentiment of this call transcript and provide a brief assessment.
        
        Call Transcript:
        {transcript_text}
        
        Provide only:
        - Overall sentiment (positive/negative/neutral)
        - Sentiment score (0-100)
        - Customer satisfaction level (high/medium/low)
        
        Keep it brief and concise.
        """
        
        recommendations_prompt = f"""
Analyze this call transcript and provide 2-3 brief, actionable recommendations for the agent.

Call Transcript:
{transcript_text}

Agent's Instructions/Purpose:
{agent_prompt if agent_prompt else "No specific instructions provided. Use general best practices for customer service calls."}

IMPORTANT - Keep recommendations BRIEF and CONCISE:
- Provide only 2-3 recommendations maximum
- Each recommendation should be 1 sentence only (brief and to the point)
- Be specific and actionable
- Use friendly, conversational tone

Format your response as:
1. [Brief recommendation in 1 sentence]
2. [Next brief recommendation in 1 sentence]
3. [Optional third recommendation in 1 sentence]

Keep it concise - similar to summary format. Maximum 1 sentence per recommendation.
"""
        
        # Helper function to generate analysis text
        def generate_analysis_text(current_model, current_api_key, prompt: str, max_tokens: int = 200):
            provider_name = (current_model.provider.name or "").strip().lower()
            
            if provider_name in ("gemini", "google", "google-ai", "google ai", "gemini-1.5-flash", "gemini-2.0-flash"):
                from app.services.gemini_service import GeminiService
                service = GeminiService()
                return service.generate_text(
                    prompt=prompt,
                    model_name=current_model.model_name,
                    temperature=0.3,
                    max_tokens=max_tokens,
                    api_key=current_api_key
                )
            elif provider_name in ("openai", "gpt", "gpt-4o-mini", "gpt-4o", "gpt-4"):
                from app.services.openai_service import OpenAIService
                service = OpenAIService()
                return service.generate_text(
                    prompt=prompt,
                    system_prompt="You are an AI assistant that analyzes call transcripts.",
                    model_name=current_model.model_name,
                    temperature=0.3,
                    max_tokens=max_tokens,
                    api_key=current_api_key
                )
            elif provider_name in ("groq", "llama", "llama-3.3-70b-versatile"):
                from app.services.groq_service import GroqService
                service = GroqService()
                return service.generate_text(
                    prompt=prompt,
                    system_prompt="You are an AI assistant that analyzes call transcripts.",
                    model_name=current_model.model_name,
                    temperature=0.3,
                    max_tokens=max_tokens,
                    api_key=current_api_key
                )
            else:
                raise ValueError(f"Unsupported provider: {provider_name}")
        
        # Generate analysis
        summary_result = None
        sentiment_result = None
        recommendations_result = None
        
        try:
            summary_result = generate_analysis_text(model, current_api_key, summary_prompt, max_tokens=200)
            sentiment_result = generate_analysis_text(model, current_api_key, sentiment_prompt, max_tokens=150)
            
            # ✅ Always generate recommendations based on transcript (not just when agent_prompt exists)
            try:
                recommendations_result = generate_analysis_text(
                    model, current_api_key, recommendations_prompt, max_tokens=300
                )
            except Exception:
                pass
        except Exception:
            return None
        
        if not summary_result or not sentiment_result:
            return None
        
        # Prepare analysis data
        analysis_data = {
            "summary": summary_result.get("content", "").strip() if summary_result else "",
            "sentiment": sentiment_result.get("content", "").strip() if sentiment_result else ""
        }
        
        # Parse recommendations
        if recommendations_result:
            recommendations_text = recommendations_result.get("content", "").strip()
            recommendations_list = []
            
            lines = recommendations_text.split('\n')
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                match = re.match(r'^\d+\.\s*(.+)$', line)
                if match:
                    recommendations_list.append(match.group(1).strip())
                elif line.startswith('- ') or line.startswith('* '):
                    recommendations_list.append(line[2:].strip())
                elif len(line) > 20 and not recommendations_list:
                    recommendations_list.append(line)
            
            if not recommendations_list:
                recommendations_list = [recommendations_text]
            
            analysis_data["recommendations"] = recommendations_list
            analysis_data["recommendations_text"] = recommendations_text
        
        return {
            "analysis": analysis_data,
            "model_used": model.model_name,
            "transcript_message_count": len(transcript_messages)
        }
        
    except Exception:
        return None


@router.post("", response_model=SuccessResponse[CSVUploadResponse])
async def upload_scheduled_calls_csv(
    file: UploadFile = File(..., description="CSV file with scheduled calls"),
    crm_config_id: str = Query(..., description="CRM configuration ID (UUID)"),
    agent_id: str = Query(..., description="Agent ID to use for all calls in this CSV (required)"),
    phone_number_id: Optional[str] = Query(None, description="Optional phone number ID to use for all calls in CSV"),
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Upload CSV file to create scheduled calls in CRM container (Monday.com, ClickUp, Jira, Trello).
    
    **CSV Format (2 columns only):**
    ```
    phone_number,call_time_utc
    ```
    
    **Required:**
    - Select CRM config (crm_config_id query parameter - required)
    - Select agent before upload (agent_id query parameter - required)
    - CSV with phone_number and call_time_utc only
    
    **Optional:**
    - phone_number_id query parameter - if provided, all calls in CSV will use this phone number
    
    **Required Columns:**
    - `phone_number`: Phone number to call (e.g., +1234567890)
    - `call_time_utc`: Scheduled time in UTC - ISO format or YYYY-MM-DD HH:MM:SS
    
    **Note:** `tenant_id` and `user_id` are automatically taken from your logged-in session.
    All calls in this CSV will use the selected CRM, agent and phone_number_id (if provided).
    
    **Example CSV:**
    ```csv
    phone_number,call_time_utc
    +1234567890,2024-12-02T14:30:00Z
    +0987654321,2024-12-02T14:31:00Z
    +1234567892,2024-12-02T14:32:00Z
    ```
    
    **Flow:**
    1. Select CRM config (crm_config_id)
    2. Select agent from dropdown
    3. Optionally select phone number (phone_number_id query param)
    4. Upload CSV (2 columns: phone_number, call_time_utc)
    5. Backend parses CSV and validates data
    6. Creates items in the user's CRM container (status: "Pending", tenant_id and phone_number_id stored)
    7. n8n cron (every 1 min) detects new items
    8. n8n waits until call_time_utc
    9. n8n calls backend `/voice/call/initiate` with phone_number_id from CRM
    10. n8n updates CRM status ("Called" or "Failed")
    
    **Data storage:** CSV rows live only in CRM. The backend stores one container
    record per user (shared by all their tenants). Items are identified by tenant_id field/column.
    """
    try:
        # Validate file type
        if not file.filename.endswith('.csv'):
            raise HTTPException(status_code=400, detail="File must be a CSV file")
        
        # Validate crm_config_id
        try:
            crm_config_uuid = uuid.UUID(crm_config_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid crm_config_id format")
        
        # Verify CRM config exists and user has active subscription for this CRM (402 if not)
        crm_config = crm_config_service.get_crm_config_by_id(db, crm_config_uuid)
        if not crm_config:
            raise HTTPException(status_code=404, detail="CRM configuration not found")
        from app.services.billing_service import BillingService
        if not BillingService.has_crm_access(db, user.id, crm_config.crm_type):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"You do not have an active subscription for {crm_config.crm_type}. Please subscribe to a plan for this CRM."
            )
        
        # Validate and verify agent_id (REQUIRED)
        try:
            agent_uuid = uuid.UUID(agent_id)
            # Verify agent exists and belongs to tenant
            agent = db.query(Agent).filter(
                and_(
                    Agent.id == agent_uuid,
                    Agent.tenant_id == user.current_tenant_id,
                    Agent.is_deleted == False
                )
            ).first()
            if not agent:
                raise HTTPException(status_code=404, detail="Agent not found or doesn't belong to tenant")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid agent_id format")
        
        # Validate phone_number_id if provided - must exist and belong to tenant
        if phone_number_id:
            try:
                phone_number_uuid = uuid.UUID(phone_number_id)
                phone_number_obj = phone_number_service.get_phone_number_by_id(
                    db=db,
                    phone_number_id=phone_number_uuid,
                    tenant_id=user.current_tenant_id
                )
                if not phone_number_obj:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Phone number {phone_number_id} not found in your account."
                    )
                if phone_number_obj.status != "active":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Phone number {phone_number_id} is not active."
                    )
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid phone_number_id format: {phone_number_id}"
                )
        
        # Read file content
        content = await file.read()
        csv_content = content.decode('utf-8')
        result = await scheduled_call_service.parse_csv_and_send_to_crm(
            db=db,
            tenant_id=user.current_tenant_id,
            user_id=user.id,
            csv_content=csv_content,
            crm_config_id=crm_config_uuid,
            default_agent_id=agent_uuid,  # Pass selected agent (required)
            default_phone_number_id=phone_number_id  # ✅ Pass phone_number_id (validated)
        )

        message = (
            f"Processed {result.total_rows} rows: {result.successful_rows} added to CRM, "
            f"{result.failed_rows} failed. Container URL: {result.board_url}"
        )

        return create_success_response(result, message)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process CSV file: {str(e)}")


@router.post("/single-call", response_model=SuccessResponse[SingleCallResponse])
async def create_single_scheduled_call(
    crm_config_id: str = Query(..., description="CRM configuration ID (UUID)"),
    agent_id: str = Query(..., description="Agent ID (UUID)"),
    phone_number: str = Query(..., description="Phone number to call (e.g., +1234567890)"),
    call_time_utc: str = Query(..., description="Scheduled time in UTC - ISO format or YYYY-MM-DD HH:MM:SS"),
    phone_number_id: Optional[str] = Query(None, description="Optional phone number ID from DB to use for call"),
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Create a single scheduled call in CRM container (Monday.com, ClickUp, Jira, Trello).
    
    **Query Parameters:**
    - `crm_config_id`: CRM configuration ID (UUID) - required
    - `agent_id`: Agent ID (UUID)
    - `phone_number`: Phone number to call (e.g., +1234567890)
    - `call_time_utc`: Scheduled time in UTC - ISO format or YYYY-MM-DD HH:MM:SS
    - `phone_number_id`: Optional phone number ID from DB to use for call
    
    **Flow:**
    1. Validates CRM config exists and belongs to tenant
    2. Validates agent exists and belongs to tenant
    3. Generates unique batch_id for this single call
    4. Creates item in user's CRM container (status: "Pending", batch_id stored, phone_number_id stored)
    5. n8n cron detects new item and triggers call at scheduled time
    6. When call completes (Called/Failed), n8n will send email for this batch
    
    **Note:** `tenant_id` and `user_id` are automatically taken from logged-in session.
    """
    try:
        # Validate crm_config_id
        try:
            crm_config_uuid = uuid.UUID(crm_config_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid crm_config_id format")
        
        # Verify CRM config exists and user has active subscription for this CRM (402 if not)
        crm_config = crm_config_service.get_crm_config_by_id(db, crm_config_uuid)
        if not crm_config:
            raise HTTPException(status_code=404, detail="CRM configuration not found")
        from app.services.billing_service import BillingService
        if not BillingService.has_crm_access(db, user.id, crm_config.crm_type):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"You do not have an active subscription for {crm_config.crm_type}. Please subscribe to a plan for this CRM."
            )
        
        # Parse agent_id
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid agent_id format")
        
        # Validate phone_number_id if provided - must exist and belong to tenant
        if phone_number_id:
            try:
                phone_number_uuid = uuid.UUID(phone_number_id)
                phone_number_obj = phone_number_service.get_phone_number_by_id(
                    db=db,
                    phone_number_id=phone_number_uuid,
                    tenant_id=user.current_tenant_id
                )
                if not phone_number_obj:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Phone number {phone_number_id} not found in your account."
                    )
                if phone_number_obj.status != "active":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Phone number {phone_number_id} is not active."
                    )
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid phone_number_id format: {phone_number_id}"
                )
        
        result = await scheduled_call_service.create_single_scheduled_call(
            db=db,
            tenant_id=user.current_tenant_id,
            user_id=user.id,
            phone_number=phone_number,
            agent_id=agent_uuid,
            call_time_utc=call_time_utc,
            crm_config_id=crm_config_uuid,
            phone_number_id=phone_number_id  # ✅ Pass phone_number_id (validated)
        )
        
        return create_success_response(
            SingleCallResponse(**result),
            result["message"]
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create scheduled call: {str(e)}")


@router.post("/from-call-session", response_model=SuccessResponse[SingleCallResponse])
async def create_scheduled_call_from_call_session(
    body: ScheduleFromCallSessionRequest = Body(...),
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db),
):
    """
    Create a scheduled call in CRM from a **completed** call session.

    User reviews the call, then hits this endpoint. Backend reads the session's transcript
    (and optional call_metadata["scheduled_call_request"]) to get date, time, and timezone or city.
    If only city is provided, timezone is resolved via city/country. Optional agent_id chooses
    which agent to use for the scheduled call; otherwise the session's agent is used.

    **Request body:**
    - `call_session_id`: UUID of the completed call session
    - `agent_id`: Optional UUID of the agent to use for the scheduled call (must belong to tenant)

    **Flow:**
    1. Validates call session exists and belongs to current tenant and is completed.
    2. Gets schedule from call_metadata["scheduled_call_request"] or extracts from transcript (LLM).
    3. Resolves timezone (explicit or from city/country).
    4. Creates one scheduled call item in the user's linked CRM board.
    """
    try:
        try:
            session_uuid = uuid.UUID(body.call_session_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid call_session_id format")
        agent_override: Optional[uuid.UUID] = None
        if body.agent_id:
            try:
                agent_override = uuid.UUID(body.agent_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid agent_id format")

        result = await scheduled_call_service.create_scheduled_call_from_call_session(
            db=db,
            call_session_id=session_uuid,
            current_tenant_id=user.current_tenant_id,
            current_user_id=user.id,
            agent_id_override=agent_override,
        )
        return create_success_response(
            SingleCallResponse(**result),
            result["message"],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create scheduled call from call session: {str(e)}")


@router.get("/crm-config", response_model=SuccessResponse[CRMConfigListResponse])
async def get_crm_config(
    user: User = Depends(require_tenant),  # Member, admin, owner — all tenant users
    db: Session = Depends(get_db)
):
    """
    Get list of all available global CRMs.
    Returns all CRM configurations with their IDs, types, and container info.
    All tenant users (member, admin, owner) can access this list for scheduled calls.
    
    **Response includes:**
    - List of all configured CRMs (Monday.com, ClickUp, Jira, Trello)
    - Each CRM shows: ID, type, display name, container info
    - User can click on any CRM to get its config_id for CSV/single call endpoints
    """
    # Get all global CRM configs (no tenant filter)
    crm_configs = crm_config_service.get_all_crm_configs(db)
    
    # CRM type display names
    crm_display_names = {
        "monday": "Monday.com",
        "clickup": "ClickUp",
        "jira": "Jira",
        "trello": "Trello"
    }
    
    configured_crms = []
    for crm_config in crm_configs:
        configured_crms.append(
            CRMConfigListItem(
                id=str(crm_config.id),
                crm_type=crm_config.crm_type,
                crm_type_display=crm_display_names.get(crm_config.crm_type, crm_config.crm_type.title()),
                container_id=crm_config.container_id,
                container_url=crm_config.container_url,
                created_at=crm_config.created_at.isoformat() if crm_config.created_at else ""
            )
        )
    
    return create_success_response(
        CRMConfigListResponse(configured_crms=configured_crms),
        f"Retrieved {len(configured_crms)} configured CRM(s)"
    )


@router.get("/board", response_model=SuccessResponse[BoardInfoResponse])
async def get_board_url(
    crm_config_id: Optional[str] = Query(None, description="CRM config ID. Pass to get that CRM's board URL; omit for first linked board."),
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db),
):
    """
    Retrieve the CRM container (board/list/project) URL.
    Pass crm_config_id to get that CRM's board URL; omit for first linked board (same as clear board items).
    """
    tenant_crm_config_uuid = None
    if crm_config_id:
        try:
            tenant_crm_config_uuid = uuid.UUID(crm_config_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid CRM config ID format")
        crm_config = crm_config_service.get_crm_config_by_id(db, tenant_crm_config_uuid)
        if not crm_config:
            raise HTTPException(status_code=404, detail="CRM config not found")
        from app.services.billing_service import BillingService
        if not BillingService.has_crm_access(db, user.id, crm_config.crm_type):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"You do not have an active subscription for {crm_config.crm_type}. Please subscribe to use this CRM.",
            )
    board_record = scheduled_call_service.get_board_for_user(db, user.id, tenant_crm_config_uuid)
    if not board_record:
        raise HTTPException(status_code=404, detail="No scheduled calls board found for this user")

    # Get container ID
    board_id = board_record.crm_container_id or board_record.monday_board_id
    if not board_id:
        raise HTTPException(status_code=404, detail="No container ID found for this user")

    # Get CRM type
    crm_type = board_record.crm_type
    if not crm_type:
        raise HTTPException(status_code=404, detail="No CRM type configured for this user")

    # Get CRM config to build proper URL using API credentials
    if not board_record.tenant_crm_config_id:
        raise HTTPException(status_code=404, detail="No CRM configuration found")
    
    crm_config = crm_config_service.get_crm_config_by_id(db, board_record.tenant_crm_config_id)
    if not crm_config:
        raise HTTPException(status_code=404, detail="CRM configuration not found")

    # Verify user has active subscription for this CRM (402 if not)
    from app.services.billing_service import BillingService
    if not BillingService.has_crm_access(db, user.id, crm_config.crm_type):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"You do not have an active subscription for {crm_config.crm_type}. Please subscribe to a plan for this CRM."
        )

    # Get CRM service using API credentials
    crm_service = CRMServiceFactory.get_service(crm_config)
    
    # CRM-specific URL fetching using API credentials
    board_url = None
    
    if crm_type.lower() == "trello":
        # Trello: Always fetch proper URL from Trello API using credentials
        # This ensures we get the correct URL with short ID and board name
        if hasattr(crm_service, 'get_board_url'):
            try:
                board_url = crm_service.get_board_url(board_id)
            except Exception:
                # Fallback to stored URL or basic URL
                board_url = board_record.crm_container_url or crm_service.build_container_url(board_id)
        else:
            board_url = board_record.crm_container_url or crm_service.build_container_url(board_id)
        # Board permissions must be set to "Observer" for view-only access
    elif crm_type.lower() == "jira":
        # Jira: Use stored URL or build from service
        board_url = board_record.crm_container_url or crm_service.build_container_url(board_id)
        # Jira: URL already points to project, view-only depends on project permissions
    elif crm_type.lower() == "monday":
        # Monday.com: Use stored URL or build from service
        board_url = board_record.crm_container_url or board_record.monday_board_url or crm_service.build_container_url(board_id)
        # Board permissions must be set to "Viewer" for view-only access
    elif crm_type.lower() == "clickup":
        # ClickUp: Always fetch proper URL from ClickUp API using credentials
        # This ensures we get the correct URL with proper format
        if hasattr(crm_service, 'get_list_url'):
            try:
                board_url = crm_service.get_list_url(board_id)
            except Exception:
                # Fallback to stored URL or basic URL
                board_url = board_record.crm_container_url or crm_service.build_container_url(board_id)
        else:
            board_url = board_record.crm_container_url or crm_service.build_container_url(board_id)
        # List permissions must be set to "Viewer" for view-only access
    else:
        # Fallback for other CRM types
        board_url = board_record.crm_container_url or crm_service.build_container_url(board_id)

    data = BoardInfoResponse(
        board_id=board_id,
        board_url=board_url,
    )
    return create_success_response(data, f"Scheduled calls board URL retrieved for {crm_type}")


@router.get(
    "/board/pending-count",
    response_model=SuccessResponse[PendingCountResponse],
    summary="Get total pending scheduled calls for current tenant across all CRMs",
)
async def get_pending_scheduled_calls_count(
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db),
):
    """
    Return total scheduled call items in **Pending** status for the current tenant
    across all CRMs the user has linked. If user has Trello (10) + Jira (5), returns 15.
    If user has only one CRM with 5 pending, returns 5. Count is tenant-specific.
    """
    from app.services.billing_service import BillingService
    from app.services.crm_config_service import CRMConfigService
    from app.services.crm_service_factory import CRMServiceFactory

    tenant_id_str = str(user.current_tenant_id)
    board_records = scheduled_call_service.get_all_boards_for_user(db, user.id)
    if not board_records:
        raise HTTPException(status_code=404, detail="No CRM boards found for this user")

    total_pending = 0
    total_items_sum = 0
    by_crm: List[PendingCountByCrm] = []
    first_board_id = ""
    first_board_url = ""

    for board_record in board_records:
        crm_type = (board_record.crm_type or "monday").lower()

        # Legacy Monday (no tenant_crm_config_id)
        if not board_record.tenant_crm_config_id:
            if crm_type != "monday":
                continue
            board_id = board_record.monday_board_id
            if not board_id:
                continue
            try:
                column_map = MondayService.ensure_required_columns(board_id)
                cnt = MondayService.count_pending_items_for_tenant_static(
                    board_id=board_id,
                    tenant_id=tenant_id_str,
                    column_map=column_map,
                    pending_label="Pending",
                )
            except Exception:
                continue
            total_pending += cnt
            total_items_sum += cnt
            by_crm.append(PendingCountByCrm(crm_type="monday", crm_config_id=None, pending_count=cnt))
            if not first_board_id:
                first_board_id = board_id
                first_board_url = board_record.monday_board_url or MondayService.build_board_url(board_id)
            continue

        crm_config = crm_config_service.get_crm_config_by_id(db, board_record.tenant_crm_config_id)
        if not crm_config:
            continue
        if not BillingService.has_crm_access(db, user.id, crm_config.crm_type):
            continue

        try:
            crm_service = CRMServiceFactory.get_service(crm_config)
            field_map = crm_service.ensure_required_fields(board_record.crm_container_id)
            cnt = crm_service.count_pending_items_for_tenant(
                container_id=board_record.crm_container_id,
                tenant_id=tenant_id_str,
                field_map=field_map,
                pending_label="Pending",
            )
        except Exception:
            continue
        total_pending += cnt
        total_items_sum += cnt
        by_crm.append(
            PendingCountByCrm(
                crm_type=crm_type,
                crm_config_id=str(board_record.tenant_crm_config_id),
                pending_count=cnt,
            )
        )
        if not first_board_id:
            first_board_id = board_record.crm_container_id or ""
            first_board_url = board_record.crm_container_url or ""

    data = PendingCountResponse(
        tenant_id=tenant_id_str,
        pending_count=total_pending,
        total_items=total_items_sum,
        by_crm=by_crm,
        board_id=first_board_id,
        board_url=first_board_url,
    )
    return create_success_response(
        data,
        f"Pending scheduled calls count: {total_pending} total across {len(by_crm)} CRM(s)" if by_crm else "No pending counts available",
    )


@router.delete("/board/items", response_model=SuccessResponse[DeleteBoardItemsResponse])
async def clear_board_items(
    crm_config_id: Optional[str] = Query(None, description="CRM config ID to clear. If omitted, first linked board is used."),
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db),
):
    """
    Remove all items belonging to the current tenant from the user's CRM container.
    Works with all CRMs (Monday.com, ClickUp, Jira, Trello).
    Only items with matching tenant_id are deleted, keeping other tenants' items intact.
    Pass crm_config_id to clear a specific CRM's board; omit for first linked board.
    """
    tenant_crm_config_uuid = None
    if crm_config_id:
        try:
            tenant_crm_config_uuid = uuid.UUID(crm_config_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid CRM config ID format")
        crm_config = crm_config_service.get_crm_config_by_id(db, tenant_crm_config_uuid)
        if not crm_config:
            raise HTTPException(status_code=404, detail="CRM config not found")
        from app.services.billing_service import BillingService
        if not BillingService.has_crm_access(db, user.id, crm_config.crm_type):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"You do not have an active subscription for {crm_config.crm_type}. Please subscribe to use this CRM.",
            )
    board_record, deleted = scheduled_call_service.clear_board_items(
        db,
        user.id,
        user.current_tenant_id,
        tenant_crm_config_id=tenant_crm_config_uuid,
    )
    
    # Use generic fields, fallback to legacy
    container_id = board_record.crm_container_id or board_record.monday_board_id
    container_url = board_record.crm_container_url or board_record.monday_board_url
    
    data = DeleteBoardItemsResponse(
        items_deleted=deleted,
        board_id=container_id,
        board_url=container_url,
    )
    return create_success_response(data, f"Deleted {deleted} item(s) for current tenant from the {board_record.crm_type} container")


@router.get("/batch/{batch_id}/analysis", response_model=SuccessResponse[dict], include_in_schema=False)
async def get_batch_analysis(
    batch_id: str,
    http_request: Request,
    tenant_id: Optional[str] = Query(None, description="Tenant ID (required when using webhook secret)"),
    user_id: Optional[str] = Query(None, description="User ID (required when using webhook secret)"),
    user: Optional[User] = Depends(get_optional_tenant_user),
    db: Session = Depends(get_db)
):
    """
    Get analysis data for a completed batch.
    Supports Monday.com, ClickUp, and Trello.
    
    **Authentication:** 
    - JWT token (default) - user and tenant from token
    - OR X-N8N-Webhook-Secret header - provide tenant_id and user_id as query params
    
    **Query Parameters (for n8n webhook):**
    - `tenant_id` (str, optional): Required when using webhook secret
    - `user_id` (str, optional): Required when using webhook secret
    
    **Returns comprehensive analysis including:**
    - Total scheduled calls (from CSV)
    - Total calls made vs pending
    - Successfully called vs failed calls
    - Total and average call duration
    - Call times and details for each call
    - Success/failure rates
    - Total cost
    - LLM transcript analysis for each call
    - Current timestamp (report generation time)
    - user_email (for n8n to send email)
    - container_id: Container ID (board/list) for n8n to update items
    - crm_type: CRM type ("monday", "clickup", or "trello")
    - email_sent_field_id: Field ID for Email Sent status update
    
    **Matching Logic:**
    - First tries to match by call_session_id from CRM items (most accurate)
    - Falls back to phone number matching if call_session_id not available
    - For Trello: Supports both custom fields and description parsing
    
    **Note:** n8n workflow should:
    1. Check batch completion on CRM (Monday.com/ClickUp/Trello)
    2. Wait 10 minutes
    3. Call this endpoint with webhook secret + tenant_id + user_id
    4. Use returned data to send email
    """
    try:
        # Verify authentication: either JWT token OR webhook secret
        is_webhook = await verify_n8n_webhook_secret_async(http_request)
        
        if is_webhook:
            # Webhook authentication - get tenant_id and user_id from query params
            if not tenant_id or not user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="tenant_id and user_id are required as query parameters when using webhook secret"
                )
            try:
                tenant_uuid = uuid.UUID(tenant_id)
                user_uuid = uuid.UUID(user_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid UUID format for tenant_id or user_id"
                )
            
            # Get user from database
            user = db.query(User).filter(User.id == user_uuid).first()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            user.current_tenant_id = tenant_uuid
        else:
            # JWT authentication - get from user token
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required: JWT token or n8n webhook secret"
                )
        
        # Get user's board
        board_record = scheduled_call_service.get_board_for_user(db, user.id)
        if not board_record:
            raise HTTPException(status_code=404, detail="Board not found for user")
        
        # Get CRM type and container ID
        crm_type = board_record.crm_type or "monday"  # Default to monday for backward compatibility
        container_id = board_record.crm_container_id or board_record.monday_board_id
        
        if not container_id:
            raise HTTPException(status_code=404, detail="Container ID not found for user")
        
        # Get CRM config and service
        crm_config = None
        if board_record.tenant_crm_config_id:
            crm_config = crm_config_service.get_crm_config_by_id(db, board_record.tenant_crm_config_id)
        
        if not crm_config:
            raise HTTPException(status_code=404, detail=f"CRM configuration not found for {crm_type}")
        
        # Get CRM service
        crm_service = CRMServiceFactory.get_service(crm_config)
        
        # Get field map
        field_map = crm_service.ensure_required_fields(container_id)
        
        # Fetch all items from CRM with this batch_id and tenant_id
        if crm_type.lower() == "monday":
            # Monday.com specific method - use instance method to get database API key
            items = crm_service.get_items_by_batch_id(
                container_id=container_id,
                batch_id=batch_id,
                tenant_id=str(user.current_tenant_id),
                field_map=field_map
            )
        elif crm_type.lower() == "clickup":
            # ClickUp specific method
            items = crm_service.get_items_by_batch_id(
                container_id=container_id,
                batch_id=batch_id,
                tenant_id=str(user.current_tenant_id),
                field_map=field_map
            )
        elif crm_type.lower() == "trello":
            # Trello specific method
            items = crm_service.get_items_by_batch_id(
                container_id=container_id,
                batch_id=batch_id,
                tenant_id=str(user.current_tenant_id),
                field_map=field_map
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Batch analysis not yet supported for CRM type: {crm_type}"
            )
        
        if not items:
            raise HTTPException(status_code=404, detail=f"No items found for batch_id: {batch_id}")
        
        # Total scheduled calls (from CRM items)
        total_scheduled = len(items)
        
        # Extract item IDs, call_session_ids and phone numbers from items
        item_ids = []  # Item IDs for CRM update
        call_session_ids = []
        phone_numbers = []
        
        for item in items:
            # Extract item ID
            item_id = item.get("id")
            if item_id:
                item_ids.append(item_id)
            
            phone_number = item.get("name", "").strip()
            if phone_number:
                phone_numbers.append(phone_number)
            
            # Extract call_session_id - both Monday.com and ClickUp use column_values format
            # (ClickUp service formats items with column_values for compatibility)
            for col_val in item.get("column_values", []):
                if col_val.get("id") == field_map.get("call_session_id"):
                    session_id = col_val.get("text", "").strip()
                    if session_id:
                        try:
                            call_session_ids.append(uuid.UUID(session_id))
                        except ValueError:
                            pass
                    break
        
        # Fetch call sessions - prefer call_session_id, fallback to phone_number
        if call_session_ids:
            # Use call_session_id for accurate matching
            call_sessions = db.query(CallSession).filter(
                and_(
                    CallSession.id.in_(call_session_ids),
                    CallSession.tenant_id == user.current_tenant_id
                )
            ).all()
        else:
            # Fallback: match by phone number
            call_sessions = db.query(CallSession).filter(
                and_(
                    CallSession.to_number.in_(phone_numbers),
                    CallSession.tenant_id == user.current_tenant_id
                )
            ).all()
        
        # Calculate statistics
        total_calls_made = len(call_sessions)  # Actually made calls
        called_count = len([cs for cs in call_sessions if cs.status == "completed"])
        failed_count = len([cs for cs in call_sessions if cs.status in ["failed", "busy"]])
        pending_count = total_scheduled - total_calls_made  # Items that never got called
        
        total_duration = sum(cs.duration or 0 for cs in call_sessions)
        avg_duration = total_duration / total_calls_made if total_calls_made > 0 else 0
        
        # Prepare call details with transcript analysis
        call_details = []
        for cs in call_sessions:
            call_detail = {
                "call_session_id": str(cs.id),
                "phone_number": cs.to_number,
                "start_time": cs.start_time.isoformat() if cs.start_time else None,
                "end_time": cs.end_time.isoformat() if cs.end_time else None,
                "duration_seconds": cs.duration,
                "duration_formatted": f"{(cs.duration or 0) // 60}m {(cs.duration or 0) % 60}s" if cs.duration else "0s",
                "status": cs.status,
                "success_evaluation": cs.success_evaluation,
                "ended_reason": cs.ended_reason,
                "cost": float(cs.cost) if cs.cost else 0.0
            }
            
            # Add transcript analysis if call is completed and has transcript
            if cs.status == "completed":
                try:
                    transcript_analysis = await analyze_call_transcript_internal(
                        db=db,
                        call_session=cs,
                        user=user
                    )
                    if transcript_analysis:
                        call_detail["transcript_analysis"] = transcript_analysis.get("analysis")
                        call_detail["analysis_model_used"] = transcript_analysis.get("model_used")
                        call_detail["transcript_message_count"] = transcript_analysis.get("transcript_message_count", 0)
                    else:
                        call_detail["transcript_analysis"] = None
                        call_detail["transcript_message_count"] = 0
                except Exception:
                    call_detail["transcript_analysis"] = None
                    call_detail["transcript_message_count"] = 0
            else:
                call_detail["transcript_analysis"] = None
                call_detail["transcript_message_count"] = 0
            
            call_details.append(call_detail)
        
        # Analysis summary
        analysis = {
            "batch_id": batch_id,
            "user_email": user.email,
            "current_time": datetime.now(timezone.utc).isoformat(),  # Report generation time
            "total_scheduled": total_scheduled,  # Total calls scheduled in CSV
            "total_calls_made": total_calls_made,  # Actually made calls
            "called": called_count,  # Successfully called
            "failed": failed_count,  # Failed calls
            "pending": pending_count,  # Never called (still Pending)
            "successful_calls": called_count,  # Alias for compatibility
            "failed_calls": failed_count,  # Alias for compatibility
            "total_duration_seconds": total_duration,
            "total_duration_formatted": f"{total_duration // 60}m {total_duration % 60}s",
            "average_duration_seconds": int(avg_duration),
            "average_duration_formatted": f"{int(avg_duration) // 60}m {int(avg_duration) % 60}s",
            "success_rate_percent": round((called_count / total_calls_made * 100) if total_calls_made > 0 else 0, 2),
            "failure_rate_percent": round((failed_count / total_calls_made * 100) if total_calls_made > 0 else 0, 2),
            "total_cost": round(sum(float(cs.cost or 0) for cs in call_sessions), 2),
            "call_details": call_details,
            "email_sent_field_id": field_map.get("email_sent"),  # Email Sent field ID for n8n to update status
            "container_id": container_id,  # Container ID (board/list/project) for n8n to update items
            "crm_type": crm_type,  # CRM type for n8n workflow
            "item_ids": item_ids  # Item IDs for CRM update
        }
        
        return create_success_response(analysis, "Batch analysis retrieved successfully")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get batch analysis: {str(e)}")


@router.post("/batch/{batch_id}/analysis/jira", response_model=SuccessResponse[dict], include_in_schema=False)
async def get_batch_analysis_jira(
    batch_id: str,
    http_request: Request,
    tenant_id: Optional[str] = Query(..., description="Tenant ID (required)"),
    user_id: Optional[str] = Query(..., description="User ID (required)"),
    db: Session = Depends(get_db),
    request_body: JiraBatchAnalysisRequest = Body(..., description="Request body with call_session_ids and other data")
):
    """
    Get analysis data for a completed batch (Jira only).
    This endpoint accepts call_session_ids directly in the request body.
    
    **Authentication:** 
    - X-N8N-Webhook-Secret header required
    
    **Query Parameters:**
    - `tenant_id` (str, required): Tenant ID
    - `user_id` (str, required): User ID
    
    **Request Body:**
    - `call_session_ids` (List[str]): List of call session IDs
    - `phone_numbers` (List[str], optional): List of phone numbers (fallback)
    - `total_scheduled` (int): Total scheduled calls
    - `item_ids` (List[str]): Item IDs for CRM update (e.g., issue_keys for Jira)
    - `container_id` (str, optional): Container ID (project_key for Jira)
    
    **Returns comprehensive analysis including:**
    - Total scheduled calls
    - Total calls made vs pending
    - Successfully called vs failed calls
    - Total and average call duration
    - Call times and details for each call
    - Success/failure rates
    - Total cost
    - LLM transcript analysis for each call
    - Current timestamp (report generation time)
    - user_email (for n8n to send email)
    - container_id: Container ID for n8n to update items
    - crm_type: "jira"
    - item_ids: Item IDs for CRM update
    """
    try:
        # Verify authentication: webhook secret required
        is_webhook = await verify_n8n_webhook_secret_async(http_request)
        
        if not is_webhook:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="X-N8N-Webhook-Secret header required"
            )
        
        # Validate tenant_id and user_id
        if not tenant_id or not user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id and user_id are required as query parameters"
            )
        
        try:
            tenant_uuid = uuid.UUID(tenant_id)
            user_uuid = uuid.UUID(user_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid UUID format for tenant_id or user_id"
            )
        
        # Get user from database
        user = db.query(User).filter(User.id == user_uuid).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.current_tenant_id = tenant_uuid
        
        # Extract data from request body (Pydantic model)
        call_session_ids = request_body.call_session_ids
        phone_numbers = request_body.phone_numbers or []
        total_scheduled = request_body.total_scheduled
        item_ids = request_body.item_ids or []
        container_id = request_body.container_id
        
        if not call_session_ids:
            raise HTTPException(
                status_code=400,
                detail="call_session_ids is required in request body"
            )
        
        if total_scheduled is None:
            raise HTTPException(
                status_code=400,
                detail="total_scheduled is required in request body"
            )
        
        # Convert call_session_ids to UUIDs
        try:
            call_session_uuids = [uuid.UUID(cs_id) for cs_id in call_session_ids if cs_id]
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid call_session_id format: {str(e)}"
            )
        
        # Fetch call sessions directly
        call_sessions = db.query(CallSession).filter(
            and_(
                CallSession.id.in_(call_session_uuids),
                CallSession.tenant_id == user.current_tenant_id
            )
        ).all()
        
        # If phone_numbers not provided, extract from call_sessions
        if not phone_numbers:
            phone_numbers = [cs.to_number for cs in call_sessions if cs.to_number]
        
        # Get field_map if container_id provided (optional, for email_sent_field_id)
        field_map = {}
        if container_id:
            try:
                # Get user's board to find CRM config
                board_record = scheduled_call_service.get_board_for_user(db, user.id)
                if board_record and board_record.tenant_crm_config_id:
                    crm_config = crm_config_service.get_crm_config_by_id(db, board_record.tenant_crm_config_id)
                    if crm_config and crm_config.crm_type.lower() == "jira":
                        crm_service = CRMServiceFactory.get_service(crm_config)
                        if isinstance(crm_service, JiraService):
                            field_map = crm_service.ensure_required_fields(container_id)
            except Exception:
                field_map = {}
        
        # Calculate statistics
        total_calls_made = len(call_sessions)
        called_count = len([cs for cs in call_sessions if cs.status == "completed"])
        failed_count = len([cs for cs in call_sessions if cs.status in ["failed", "busy"]])
        pending_count = total_scheduled - total_calls_made
        
        total_duration = sum(cs.duration or 0 for cs in call_sessions)
        avg_duration = total_duration / total_calls_made if total_calls_made > 0 else 0
        
        # Prepare call details with transcript analysis
        call_details = []
        for cs in call_sessions:
            call_detail = {
                "call_session_id": str(cs.id),
                "phone_number": cs.to_number,
                "start_time": cs.start_time.isoformat() if cs.start_time else None,
                "end_time": cs.end_time.isoformat() if cs.end_time else None,
                "duration_seconds": cs.duration,
                "duration_formatted": f"{(cs.duration or 0) // 60}m {(cs.duration or 0) % 60}s" if cs.duration else "0s",
                "status": cs.status,
                "success_evaluation": cs.success_evaluation,
                "ended_reason": cs.ended_reason,
                "cost": float(cs.cost) if cs.cost else 0.0
            }
            
            # Add transcript analysis if call is completed and has transcript
            if cs.status == "completed":
                try:
                    transcript_analysis = await analyze_call_transcript_internal(
                        db=db,
                        call_session=cs,
                        user=user
                    )
                    if transcript_analysis:
                        call_detail["transcript_analysis"] = transcript_analysis.get("analysis")
                        call_detail["analysis_model_used"] = transcript_analysis.get("model_used")
                        call_detail["transcript_message_count"] = transcript_analysis.get("transcript_message_count", 0)
                    else:
                        call_detail["transcript_analysis"] = None
                        call_detail["transcript_message_count"] = 0
                except Exception:
                    call_detail["transcript_analysis"] = None
                    call_detail["transcript_message_count"] = 0
            else:
                call_detail["transcript_analysis"] = None
                call_detail["transcript_message_count"] = 0
            
            call_details.append(call_detail)
        
        # Analysis summary
        analysis = {
            "batch_id": batch_id,
            "user_email": user.email,
            "current_time": datetime.now(timezone.utc).isoformat(),
            "total_scheduled": total_scheduled,
            "total_calls_made": total_calls_made,
            "called": called_count,
            "failed": failed_count,
            "pending": pending_count,
            "successful_calls": called_count,
            "failed_calls": failed_count,
            "total_duration_seconds": total_duration,
            "total_duration_formatted": f"{total_duration // 60}m {total_duration % 60}s",
            "average_duration_seconds": int(avg_duration),
            "average_duration_formatted": f"{int(avg_duration) // 60}m {int(avg_duration) % 60}s",
            "success_rate_percent": round((called_count / total_calls_made * 100) if total_calls_made > 0 else 0, 2),
            "failure_rate_percent": round((failed_count / total_calls_made * 100) if total_calls_made > 0 else 0, 2),
            "total_cost": round(sum(float(cs.cost or 0) for cs in call_sessions), 2),
            "call_details": call_details,
            "email_sent_field_id": field_map.get("email_sent") if field_map else None,
            "container_id": container_id or "N/A",
            "crm_type": "jira",
            "item_ids": item_ids or []
        }
        
        return create_success_response(analysis, "Batch analysis retrieved successfully for Jira")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get batch analysis: {str(e)}")


@router.post("/batch/{batch_id}/mark-email-sent", response_model=SuccessResponse[dict], include_in_schema=False)
async def mark_batch_email_sent(
    batch_id: str,
    http_request: Request,
    tenant_id: Optional[str] = Query(None, description="Tenant ID (required when using webhook secret)"),
    user_id: Optional[str] = Query(None, description="User ID (required when using webhook secret)"),
    user: Optional[User] = Depends(get_optional_tenant_user),
    db: Session = Depends(get_db)
):
    """
    Mark Email Sent status as "Yes" for all items in a batch (Monday.com only).
    This endpoint is hidden from Swagger schema.
    
    **Authentication:** 
    - JWT token (default) - user and tenant from token
    - OR X-N8N-Webhook-Secret header - provide tenant_id and user_id as query params
    
    **Query Parameters (for n8n webhook):**
    - `tenant_id` (str, optional): Required when using webhook secret
    - `user_id` (str, optional): Required when using webhook secret
    
    **Returns:**
    - batch_id: Batch ID that was updated
    - items_updated: Number of items successfully updated
    - total_items: Total items found in batch
    
    **Note:** This endpoint:
    1. Fetches items by batch_id and tenant_id
    2. Updates Email Sent column to "Yes" for all matching items
    3. Returns count of updated items
    """
    try:
        # Verify authentication: either JWT token OR webhook secret
        is_webhook = await verify_n8n_webhook_secret_async(http_request)
        
        if is_webhook:
            # Webhook authentication - get tenant_id and user_id from query params
            if not tenant_id or not user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="tenant_id and user_id are required as query parameters when using webhook secret"
                )
            try:
                tenant_uuid = uuid.UUID(tenant_id)
                user_uuid = uuid.UUID(user_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid UUID format for tenant_id or user_id"
                )
            
            # Get user from database
            user = db.query(User).filter(User.id == user_uuid).first()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            user.current_tenant_id = tenant_uuid
        else:
            # JWT authentication - get from user token
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required: JWT token or n8n webhook secret"
                )
        
        # Get user's board
        board_record = scheduled_call_service.get_board_for_user(db, user.id)
        if not board_record:
            raise HTTPException(status_code=404, detail="Board not found for user")
        
        # Get CRM config and service (uses database API key)
        crm_config = None
        if board_record.tenant_crm_config_id:
            crm_config = crm_config_service.get_crm_config_by_id(db, board_record.tenant_crm_config_id)
        
        if not crm_config:
            raise HTTPException(status_code=404, detail="CRM configuration not found for user's board")
        
        # Get CRM service instance (uses database API key)
        try:
            crm_service = CRMServiceFactory.get_service(crm_config)
        except Exception:
            raise
        
        # Verify it's Monday.com service
        if not isinstance(crm_service, MondayService):
            raise HTTPException(status_code=400, detail=f"This endpoint is for Monday.com only. Current CRM type: {crm_config.crm_type}")
        
        # Get container ID
        container_id = board_record.crm_container_id or board_record.monday_board_id
        if not container_id:
            raise HTTPException(status_code=404, detail="Container ID not found for user")
        
        # Get field map using instance method (uses database API key)
        try:
            field_map = crm_service.ensure_required_fields(container_id)
        except Exception:
            raise
        
        # Fetch items by batch_id and tenant_id using instance method
        try:
            items = crm_service.get_items_by_batch_id(
                container_id=container_id,
            batch_id=batch_id,
            tenant_id=str(user.current_tenant_id),
                field_map=field_map
            )
        except Exception:
            raise
        
        if not items:
            raise HTTPException(status_code=404, detail=f"No items found for batch_id: {batch_id}")
        
        # Get email_sent column ID
        email_sent_column_id = field_map.get("email_sent")
        if not email_sent_column_id:
            raise HTTPException(status_code=500, detail="Email Sent column not found in board")
        
        # Extract item IDs
        item_ids = [item["id"] for item in items]
        
        # Update Email Sent status to "Yes" using instance method (uses database API key)
        try:
            updated_count = crm_service.update_items_email_sent(
                container_id=container_id,
            item_ids=item_ids,
                field_map=field_map
            )
        except Exception:
            raise
        
        result = {
            "batch_id": batch_id,
            "items_updated": updated_count,
            "total_items": len(item_ids)
        }
        
        return create_success_response(
            result,
            f"Successfully marked {updated_count} item(s) as email sent"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to mark email sent: {str(e)}")


@router.post("/batch/{batch_id}/mark-email-sent/clickup", response_model=SuccessResponse[dict], include_in_schema=False)
async def mark_batch_email_sent_clickup(
    batch_id: str,
    http_request: Request,
    tenant_id: Optional[str] = Query(None, description="Tenant ID (required when using webhook secret)"),
    user_id: Optional[str] = Query(None, description="User ID (required when using webhook secret)"),
    user: Optional[User] = Depends(get_optional_tenant_user),
    db: Session = Depends(get_db)
):
    """
    Mark Email Sent status as "Yes" for all items in a batch (ClickUp only).
    
    **Authentication:** 
    - JWT token (default) - user and tenant from token
    - OR X-N8N-Webhook-Secret header - provide tenant_id and user_id as query params
    
    **Query Parameters (for n8n webhook):**
    - `tenant_id` (str, optional): Required when using webhook secret
    - `user_id` (str, optional): Required when using webhook secret
    
    **Returns:**
    - batch_id: Batch ID that was updated
    - items_updated: Number of items successfully updated
    - total_items: Total items found in batch
    
    **Note:** This endpoint:
    1. Fetches items by batch_id and tenant_id from ClickUp
    2. Updates Email Sent field to "Yes" for all matching tasks
    3. Returns count of updated items
    """
    try:
        # Verify authentication: either JWT token OR webhook secret
        is_webhook = await verify_n8n_webhook_secret_async(http_request)
        
        if is_webhook:
            # Webhook authentication - get tenant_id and user_id from query params
            if not tenant_id or not user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="tenant_id and user_id are required as query parameters when using webhook secret"
                )
            try:
                tenant_uuid = uuid.UUID(tenant_id)
                user_uuid = uuid.UUID(user_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid UUID format for tenant_id or user_id"
                )
            
            # Get user from database
            user = db.query(User).filter(User.id == user_uuid).first()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            user.current_tenant_id = tenant_uuid
        else:
            # JWT authentication - get from user token
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required: JWT token or n8n webhook secret"
                )
        
        # Get user's board
        board_record = scheduled_call_service.get_board_for_user(db, user.id)
        if not board_record:
            raise HTTPException(status_code=404, detail="Board not found for user")
        
        # Verify CRM type is ClickUp
        crm_type = board_record.crm_type or "monday"
        if crm_type.lower() != "clickup":
            raise HTTPException(
                status_code=400,
                detail=f"This endpoint is for ClickUp only. Current CRM type: {crm_type}"
            )
        
        # Get CRM config
        crm_config = crm_config_service.get_crm_config_by_id(db, board_record.tenant_crm_config_id)
        if not crm_config:
            raise HTTPException(status_code=404, detail="CRM configuration not found for user's board")
        
        # Get ClickUp service
        crm_service = CRMServiceFactory.get_service(crm_config)
        if not isinstance(crm_service, ClickUpService):
            raise HTTPException(status_code=500, detail="ClickUp service not available")
        
        # Get container ID
        container_id = board_record.crm_container_id
        if not container_id:
            raise HTTPException(status_code=404, detail="Container ID not found for user")
        
        # Get field map
        field_map = crm_service.ensure_required_fields(container_id)
        
        # Fetch items by batch_id and tenant_id
        items = crm_service.get_items_by_batch_id(
            container_id=container_id,
            batch_id=batch_id,
            tenant_id=str(user.current_tenant_id),
            field_map=field_map
        )
        
        if not items:
            raise HTTPException(status_code=404, detail=f"No items found for batch_id: {batch_id}")
        
        # Get email_sent field ID
        email_sent_field_id = field_map.get("email_sent")
        if not email_sent_field_id:
            raise HTTPException(status_code=500, detail="Email Sent field not found in container")
        
        # Extract item IDs
        item_ids = [item["id"] for item in items]
        
        # Update Email Sent status to "Yes" for all items
        updated_count = crm_service.update_items_email_sent(
            container_id=container_id,
            item_ids=item_ids,
            field_map=field_map
        )
        
        result = {
            "batch_id": batch_id,
            "items_updated": updated_count,
            "total_items": len(item_ids)
        }
        
        return create_success_response(
            result,
            f"Successfully marked {updated_count} item(s) as email sent in ClickUp"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to mark email sent: {str(e)}")


@router.post("/batch/{batch_id}/mark-email-sent/trello", response_model=SuccessResponse[dict], include_in_schema=False)
async def mark_batch_email_sent_trello(
    batch_id: str,
    http_request: Request,
    tenant_id: Optional[str] = Query(None, description="Tenant ID (required when using webhook secret)"),
    user_id: Optional[str] = Query(None, description="User ID (required when using webhook secret)"),
    user: Optional[User] = Depends(get_optional_tenant_user),
    db: Session = Depends(get_db)
):
    """
    Mark Email Sent status as "Yes" for all items in a batch (Trello only).
    
    **Authentication:** 
    - JWT token (default) - user and tenant from token
    - OR X-N8N-Webhook-Secret header - provide tenant_id and user_id as query params
    
    **Query Parameters (for n8n webhook):**
    - `tenant_id` (str, optional): Required when using webhook secret
    - `user_id` (str, optional): Required when using webhook secret
    
    **Returns:**
    - batch_id: Batch ID that was updated
    - items_updated: Number of items successfully updated
    - total_items: Total items found in batch
    
    **Note:** This endpoint:
    1. Fetches items by batch_id and tenant_id from Trello
    2. Updates Email Sent status to "Yes" for all matching cards (custom field or description)
    3. Returns count of updated items
    """
    try:
        # Verify authentication: either JWT token OR webhook secret
        is_webhook = await verify_n8n_webhook_secret_async(http_request)
        
        if is_webhook:
            # Webhook authentication - get tenant_id and user_id from query params
            if not tenant_id or not user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="tenant_id and user_id are required as query parameters when using webhook secret"
                )
            try:
                tenant_uuid = uuid.UUID(tenant_id)
                user_uuid = uuid.UUID(user_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid UUID format for tenant_id or user_id"
                )
            
            # Get user from database
            user = db.query(User).filter(User.id == user_uuid).first()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            user.current_tenant_id = tenant_uuid
        else:
            # JWT authentication - get from user token
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required: JWT token or n8n webhook secret"
                )
        
        # Get user's board
        board_record = scheduled_call_service.get_board_for_user(db, user.id)
        if not board_record:
            raise HTTPException(status_code=404, detail="Board not found for user")
        
        # Get CRM type and container ID
        crm_type = board_record.crm_type or "trello"
        container_id = board_record.crm_container_id
        
        if not container_id:
            raise HTTPException(status_code=404, detail="Container ID not found for user")
        
        if crm_type.lower() != "trello":
            raise HTTPException(
                status_code=400,
                detail=f"This endpoint is only for Trello CRM. Current CRM type: {crm_type}"
            )
        
        # Get CRM config
        crm_config = None
        if board_record.tenant_crm_config_id:
            crm_config = crm_config_service.get_crm_config_by_id(db, board_record.tenant_crm_config_id)
        
        if not crm_config:
            raise HTTPException(status_code=404, detail="CRM configuration not found for user's board")
        
        # Get Trello service
        crm_service = CRMServiceFactory.get_service(crm_config)
        if not isinstance(crm_service, TrelloService):
            raise HTTPException(status_code=500, detail="Trello service not available")
        
        # Get field map
        field_map = crm_service.ensure_required_fields(container_id)
        
        # Fetch items by batch_id and tenant_id
        items = crm_service.get_items_by_batch_id(
            container_id=container_id,
            batch_id=batch_id,
            tenant_id=str(user.current_tenant_id),
            field_map=field_map
        )
        
        if not items:
            raise HTTPException(status_code=404, detail=f"No items found for batch_id: {batch_id}")
        
        # Extract item IDs
        item_ids = [item["id"] for item in items]
        
        # Update Email Sent status to "Yes" for all items
        updated_count = crm_service.update_items_email_sent(
            container_id=container_id,
            item_ids=item_ids,
            field_map=field_map
        )
        
        result = {
            "batch_id": batch_id,
            "items_updated": updated_count,
            "total_items": len(item_ids)
        }
        
        return create_success_response(
            result,
            f"Successfully marked {updated_count} item(s) as email sent in Trello"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to mark email sent: {str(e)}")


@router.post("/batch/{batch_id}/mark-email-sent/jira", response_model=SuccessResponse[dict], include_in_schema=False)
async def mark_batch_email_sent_jira(
    batch_id: str,
    http_request: Request,
    tenant_id: Optional[str] = Query(None, description="Tenant ID (required when using webhook secret)"),
    user_id: Optional[str] = Query(None, description="User ID (required when using webhook secret)"),
    user: Optional[User] = Depends(get_optional_tenant_user),
    db: Session = Depends(get_db),
    issue_keys: List[str] = Body(..., description="List of Jira issue keys to update")
):
    """
    Mark Email Sent status as "Yes" for specific Jira issues in a batch.
    
    **Authentication:** 
    - JWT token (default) - user and tenant from token
    - OR X-N8N-Webhook-Secret header - provide tenant_id and user_id as query params
    
    **Query Parameters (for n8n webhook):**
    - `tenant_id` (str, optional): Required when using webhook secret
    - `user_id` (str, optional): Required when using webhook secret
    
    **Request Body:**
    - `issue_keys` (List[str]): List of Jira issue keys (e.g., ["SCHEDU7-1", "SCHEDU7-2"])
    
    **Returns:**
    - batch_id: Batch ID that was updated
    - items_updated: Number of items successfully updated
    - total_items: Total items found in batch
    
    **Note:** This endpoint:
    1. Accepts issue_keys directly in request body (no need to fetch by batch_id)
    2. Updates Email Sent status to "Yes" in description for all matching issues
    3. Returns count of updated issues
    """
    try:
        # Verify authentication: either JWT token OR webhook secret
        is_webhook = await verify_n8n_webhook_secret_async(http_request)
        
        if is_webhook:
            # Webhook authentication - get tenant_id and user_id from query params
            if not tenant_id or not user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="tenant_id and user_id are required as query parameters when using webhook secret"
                )
            try:
                tenant_uuid = uuid.UUID(tenant_id)
                user_uuid = uuid.UUID(user_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid UUID format for tenant_id or user_id"
                )
            
            # Get user from database
            user = db.query(User).filter(User.id == user_uuid).first()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            user.current_tenant_id = tenant_uuid
        else:
            # JWT authentication - get from user token
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required: JWT token or n8n webhook secret"
                )
        
        # Get user's board
        board_record = scheduled_call_service.get_board_for_user(db, user.id)
        if not board_record:
            raise HTTPException(status_code=404, detail="Board not found for user")
        
        # Verify CRM type is Jira
        crm_type = board_record.crm_type or "monday"
        if crm_type.lower() != "jira":
            raise HTTPException(
                status_code=400,
                detail=f"This endpoint is for Jira only. Current CRM type: {crm_type}"
            )
        
        # Get CRM config
        crm_config = crm_config_service.get_crm_config_by_id(db, board_record.tenant_crm_config_id)
        if not crm_config:
            raise HTTPException(status_code=404, detail="CRM configuration not found for user's board")
        
        # Get Jira service
        crm_service = CRMServiceFactory.get_service(crm_config)
        if not isinstance(crm_service, JiraService):
            raise HTTPException(status_code=500, detail="Jira service not available")
        
        # Get container ID
        container_id = board_record.crm_container_id
        if not container_id:
            raise HTTPException(status_code=404, detail="Container ID not found for user")
        
        # Get field map
        field_map = crm_service.ensure_required_fields(container_id)
        
        # Use issue_keys directly from request body (no need to fetch by batch_id)
        if not issue_keys:
            raise HTTPException(
                status_code=400,
                detail="issue_keys is required in request body"
            )
        
        # Update Email Sent status to "Yes" for all items
        updated_count = crm_service.update_items_email_sent(
            container_id=container_id,
            item_ids=issue_keys,  # Use issue_keys directly
            field_map=field_map
        )
        
        result = {
            "batch_id": batch_id,
            "items_updated": updated_count,
            "total_items": len(issue_keys)
        }
        
        return create_success_response(
            result,
            f"Successfully marked {updated_count} Jira issue(s) as email sent"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to mark email sent: {str(e)}")


@router.post("/select-crm-config", response_model=SuccessResponse[dict])
async def select_crm_config(
    crm_config_id: str = Query(..., description="CRM configuration ID (UUID)"),
    user: User = Depends(require_owner),  # Only owner can select
    db: Session = Depends(get_db)
):
    """
    Owner tenant links user with one CRM config (multi-CRM: call once per CRM to link).
    If user is already linked with this CRM, returns 400.
    
    **Authentication:** JWT token required (owner role only)
    
    **Query params:** `crm_config_id=uuid`
    """
    if not user.current_tenant_id:
        raise HTTPException(status_code=400, detail="No tenant selected")
    try:
        try:
            crm_config_uuid = uuid.UUID(crm_config_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid CRM config ID format")
        # Already linked with this CRM?
        existing = scheduled_call_service.get_board_for_user(db, user.id, crm_config_uuid)
        if existing:
            raise HTTPException(
                status_code=400,
                detail="Already linked with this CRM config. Each CRM can be linked only once per user.",
            )
        # Verify CRM config exists
        crm_config = crm_config_service.get_crm_config_by_id(db, crm_config_uuid)
        if not crm_config:
            raise HTTPException(status_code=404, detail="CRM config not found")
        # Require active subscription for this CRM (402 if not)
        from app.services.billing_service import BillingService
        if not BillingService.has_crm_access(db, user.id, crm_config.crm_type):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"You do not have an active subscription for {crm_config.crm_type}. Please subscribe to a plan for this CRM before selecting it."
            )
        # Get or create board for this user + CRM config (creates container on first use)
        board_record, _ = scheduled_call_service.get_or_create_board_for_user(
            db=db, user_id=user.id, tenant_id=user.current_tenant_id, crm_config_id=crm_config_uuid
        )
        result = {
            "crm_config_id": str(crm_config_uuid),
            "crm_type": crm_config.crm_type,
            "container_id": board_record.crm_container_id,
            "container_url": board_record.crm_container_url,
        }
        return create_success_response(result, f"CRM config '{crm_config.crm_type}' linked successfully.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to select CRM config: {str(e)}")


@router.get("/selected-crm-config", response_model=SuccessResponse[dict])
async def get_selected_crm_config(
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Get all CRM configs linked with this user (multi-CRM support).
    Returns list of linked CRMs with crm_config_id, crm_type, container_id, container_url.
    
    **Authentication:** JWT token required
    """
    try:
        # All board records for user (one per linked CRM)
        board_records = db.query(ScheduledCall).filter(ScheduledCall.user_id == user.id).order_by(ScheduledCall.crm_type).all()
        linked_crms: List[Dict[str, Any]] = []
        for board_record in board_records:
            if not board_record.tenant_crm_config_id:
                continue
            crm_config = crm_config_service.get_crm_config_by_id(db, board_record.tenant_crm_config_id)
            linked_crms.append({
                "crm_config_id": str(board_record.tenant_crm_config_id),
                "crm_type": board_record.crm_type or (crm_config.crm_type if crm_config else None),
                "container_id": board_record.crm_container_id,
                "container_url": board_record.crm_container_url,
            })
        return create_success_response(
            {"linked_crms": linked_crms, "count": len(linked_crms)},
            f"Retrieved {len(linked_crms)} linked CRM config(s)" if linked_crms else "No CRM configs linked yet"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get selected CRM config: {str(e)}")


@router.get("/jira-credentials", response_model=SuccessResponse[dict], include_in_schema=False)
async def get_jira_credentials(
    http_request: Request,
    tenant_id: Optional[str] = Query(None, description="Tenant ID (optional - if not provided, returns all Jira users)"),
    user_id: Optional[str] = Query(None, description="User ID (optional - if not provided, returns all Jira users)"),
    user: Optional[User] = Depends(get_optional_tenant_user),
    db: Session = Depends(get_db)
):
    """
    Get Jira credentials for n8n automation.
    
    **Two modes:**
    1. **Single User Mode:** If tenant_id and user_id provided, returns credentials for that user
    2. **All Users Mode:** If tenant_id and user_id NOT provided, returns list of all users with Jira configured
    
    **Authentication:** 
    - JWT token (default) - user and tenant from token
    - OR X-N8N-Webhook-Secret header - provide tenant_id and user_id as query params (optional)
    
    **Query Parameters (for n8n webhook):**
    - `tenant_id` (str, optional): If provided, returns single user credentials
    - `user_id` (str, optional): If provided, returns single user credentials
    
    **Returns (Single User Mode):**
    - api_token: Decrypted Jira API token
    - email: Jira account email
    - server_url: Jira server URL
    - project_key: User's Jira project key
    - user_id: User ID
    - tenant_id: Tenant ID
    
    **Returns (All Users Mode):**
    - api_token: Decrypted Jira API token (global)
    - email: Jira account email (global)
    - server_url: Jira server URL (global)
    - users: Array of users with Jira configured
      - user_id: User ID
      - tenant_id: Tenant ID
      - project_key: User's project key
    """
    try:
        # Verify authentication: either JWT token OR webhook secret
        is_webhook = await verify_n8n_webhook_secret_async(http_request)
        
        # Get Jira CRM config (global config, same for all users)
        jira_config = crm_config_service.get_crm_config_by_type(db, "jira")
        
        if not jira_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Jira CRM config not found. Please configure Jira first."
            )
        
        # Decrypt API token
        from app.core.security import decrypt_api_key
        api_token = decrypt_api_key(jira_config.encrypted_api_key)
        
        # Parse additional_config for email and server_url
        import json
        additional_config = {}
        if jira_config.additional_config:
            additional_config = json.loads(jira_config.additional_config)
        
        email = additional_config.get("email", "")
        server_url = additional_config.get("server_url", "")
        
        if not email or not server_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Jira email or server_url not configured in additional_config"
            )
        
        # If tenant_id and user_id provided, return single user credentials
        if tenant_id and user_id:
            if is_webhook:
                try:
                    tenant_uuid = uuid.UUID(tenant_id)
                    user_uuid = uuid.UUID(user_id)
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid UUID format for tenant_id or user_id"
                    )
                
                # Get user from database
                user = db.query(User).filter(User.id == user_uuid).first()
                if not user:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="User not found"
                    )
            else:
                # JWT authentication - user already available from Depends
                if not user:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Authentication required"
                    )
            
            # Get board record for user (contains crm_container_id = project_key)
            board_record = scheduled_call_service.get_board_for_user(db, user.id)
            
            if not board_record or not board_record.tenant_crm_config_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No Jira CRM config selected for this user. Please select a Jira CRM config first."
                )
            
            # Verify it's Jira
            if board_record.crm_type != "jira":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Selected CRM is {board_record.crm_type}, not Jira"
                )
            
            # Get project key from crm_container_id (dynamically created per user)
            project_key = board_record.crm_container_id
            
            if not project_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Jira project key not found. Please upload a CSV first to create the project."
                )
            
            # Get tenant_id (use current_tenant_id or first tenant)
            tenant_id_value = str(user.current_tenant_id) if user.current_tenant_id else None
            if not tenant_id_value and user.tenants:
                tenant_id_value = str(user.tenants[0].id)
            
            result = {
                "email": email,
                "server_url": server_url,
                "project_key": project_key,
                "user_id": str(user.id),
                "tenant_id": tenant_id_value
            }
            
            return create_success_response(
                result,
                "Jira credentials retrieved successfully"
            )
        
        # If tenant_id and user_id NOT provided, return all users with Jira configured
        else:
            # Get all ScheduledCall records with Jira CRM type
            from app.models.scheduled_call import ScheduledCall
            jira_boards = db.query(ScheduledCall).filter(
                ScheduledCall.crm_type == "jira",
                ScheduledCall.tenant_crm_config_id.isnot(None),
                ScheduledCall.crm_container_id.isnot(None)
            ).all()
            
            users_list = []
            for board_record in jira_boards:
                # Get user
                user = db.query(User).filter(User.id == board_record.user_id).first()
                if not user:
                    continue
                
                # Get all tenants for this user
                user_tenants = user.tenants
                if not user_tenants:
                    continue
                
                # For each tenant, add an entry (no filtering - return all projects)
                for tenant in user_tenants:
                    users_list.append({
                        "user_id": str(user.id),
                        "tenant_id": str(tenant.id),
                        "project_key": board_record.crm_container_id
                    })
            
            result = {
                "email": email,
                "server_url": server_url,
                "users": users_list,
                "total": len(users_list)
            }
            
            return create_success_response(
                result,
                f"Retrieved {len(users_list)} user(s) with Jira configured"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get Jira credentials: {str(e)}"
        )
 
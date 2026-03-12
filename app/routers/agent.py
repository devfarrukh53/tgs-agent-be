from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import Optional
from app.schemas.agent import AgentCreate, AgentUpdate, AgentOut, AgentListResponse, LanguageEnum, VoiceTypeEnum
from app.api.deps import (
    get_db,
    get_current_user_jwt,
    require_member_or_admin,
    require_tenant,
    require_admin_or_owner,
)
from app.schemas.agent import AgentCreate, AgentUpdate, AgentOut, AgentListResponse
from app.schemas.base import SuccessResponse
from app.schemas.prompt_engineer import PromptEngineerRequest, PromptEngineerResult
from app.services.agent_service import agent_service
from app.services.openai_service import openai_service
from app.services.credit_service import credit_service
from app.services.model_service import model_service
from app.core.security import decrypt_api_key
from app.models.user import User
from app.utils.response import create_success_response
from app.core.logger import logger
import uuid
import json

router = APIRouter()


@router.post("/", response_model=SuccessResponse[AgentOut], status_code=status.HTTP_201_CREATED)
def create_agent(
    agent_in: AgentCreate,
    tenant_user: User = Depends(require_tenant),  # ← First middleware: tenant validation
    admin_user: User = Depends(require_admin_or_owner),    # ← Second middleware: admin validation
    db: Session = Depends(get_db)
):
    """Create a new agent"""
    # Trim whitespace from agent name
    agent_in.name = " ".join(agent_in.name.split())
    # Both tenant_user and admin_user are validated by their respective middleware
    # We can use either one since they both represent the same user
    agent = agent_service.create_agent(db, agent_in, admin_user.current_tenant_id, admin_user.id)
    return create_success_response(agent, "Agent created successfully", status.HTTP_201_CREATED)


@router.get("/{agent_id}", response_model=SuccessResponse[AgentOut])
def get_agent(
    agent_id: uuid.UUID,
    tenant_user: User = Depends(require_tenant),  # ← First middleware: tenant validation
    user: User = Depends(require_member_or_admin),    # ← Second middleware: admin validation
    db: Session = Depends(get_db)
):
    """Get a specific agent by ID"""
    agent = agent_service.get_agent_by_id(db, agent_id, user.current_tenant_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    return create_success_response(agent, "Agent retrieved successfully")


@router.get("/", response_model=SuccessResponse[AgentListResponse])
def list_agents(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(10, ge=1, le=100, description="Records per page"),
    search: Optional[str] = Query(None, description="Search by name"),
    tenant_user: User = Depends(require_tenant),  # ← First middleware: tenant validation
    user: User = Depends(require_member_or_admin),    # ← Second middleware: admin validation
    db: Session = Depends(get_db)
):
    """Get agents with pagination and search"""
    agents = agent_service.list_agents(db, user.current_tenant_id, page, limit, search)
    return create_success_response(agents, "Agents retrieved successfully")


@router.put("/{agent_id}", response_model=SuccessResponse[AgentOut])
def update_agent(
    agent_id: uuid.UUID,
    agent_update: AgentUpdate,
    tenant_user: User = Depends(require_tenant),  # ← First middleware: tenant validation
    admin_user: User = Depends(require_admin_or_owner),    # ← Second middleware: admin validation
    db: Session = Depends(get_db)
):
    """Update an agent"""
    agent = agent_service.update_agent(db, agent_id, agent_update, admin_user.current_tenant_id, admin_user.id)
    return create_success_response(agent, "Agent updated successfully")


@router.delete("/{agent_id}", response_model=SuccessResponse[dict])
def delete_agent(
    agent_id: uuid.UUID,
    tenant_user: User = Depends(require_tenant),  # ← First middleware: tenant validation
    admin_user: User = Depends(require_admin_or_owner),    # ← Second middleware: admin validation
    db: Session = Depends(get_db)
):
    """Delete an agent"""
    agent_service.delete_agent(db, agent_id, admin_user.current_tenant_id)
    return create_success_response({"id": str(agent_id)}, "Agent deleted successfully")


@router.get("/search/{search_term}", response_model=SuccessResponse[list[AgentOut]])
def search_agents(
    search_term: str,
    tenant_user: User = Depends(require_tenant),  # ← First middleware: tenant validation
    user: User = Depends(require_member_or_admin),    # ← Second middleware: admin validation
    db: Session = Depends(get_db)
):
    """Search agents by name"""
    agents = agent_service.search_agents(db, user.current_tenant_id, search_term)
    agent_list = [AgentOut.model_validate(agent) for agent in agents]
    return create_success_response(agent_list, f"Found {len(agent_list)} agents matching '{search_term}'") 

@router.get("/meta/voice-options")
def get_voice_options(
    user: User = Depends(get_current_user_jwt),
):
    return {
        "voice_types": [v.value for v in VoiceTypeEnum],
        "languages": [l.value for l in LanguageEnum],
    }


@router.get("/{agent_id}/model-config")
def get_agent_model_config(
    agent_id: uuid.UUID,
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Get the effective model configuration for an agent.
    Returns agent-specific values if set, otherwise falls back to model defaults.
    """
    try:
        config = agent_service.get_agent_effective_model_config(db, agent_id, user.current_tenant_id)
        return create_success_response(
            config,
            "Agent model configuration retrieved successfully"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get agent model configuration: {str(e)}"
        )


@router.get("/{agent_id}/talk")
async def get_talk_to_assistant_link(
    agent_id: uuid.UUID,
    user: User = Depends(require_tenant),
    db: Session = Depends(get_db)
):
    """
    Get the "Talk to Assistant" link for an agent
    """
    try:
        # Validate agent exists and belongs to user's tenant
        agent = agent_service.get_agent_by_id(db, agent_id, user.current_tenant_id)
        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent not found"
            )
        
        # Return the talk link
        talk_url = f"/api/v1/live-voice/talk/{agent_id}"
        
        return create_success_response(
            {
                "agent_id": str(agent.id),
                "agent_name": agent.name,
                "talk_url": talk_url,
                "status": "ready"
            },
            f"Talk to {agent.name} link generated"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate talk link: {str(e)}"
        )


@router.post(
    "/prompt-engineer",
    response_model=SuccessResponse[PromptEngineerResult],
    status_code=status.HTTP_200_OK,
)
async def design_agent_prompt(
    request: PromptEngineerRequest,
    tenant_user: User = Depends(require_tenant),
    user: User = Depends(require_member_or_admin),
    db: Session = Depends(get_db),
):
    """
    Generate or refine a production-ready system prompt for an agent.

    - Accepts the user's natural-language requirement in any language.
    - Returns either clarifying questions or a final, structured system prompt.
    - Uses OpenAI `gpt-4o-mini` under the hood.
    - Deducts 0.5 credits per successful AI response from the tenant.
    """
    try:
        if not user.current_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tenant selected. Please set a current tenant.",
            )

        tenant_id = user.current_tenant_id

        # ------------------------------------------------------------------
        # System prompt for the internal "prompt engineer" assistant
        # Structured using best practices from OpenAI prompt engineering docs.
        # ------------------------------------------------------------------
        system_prompt = """
You are an expert prompt engineer for a multi-tenant SaaS product that builds AI voice agents for phone calls.
Your job is to take a user's requirement (in any language) and design a production-ready SYSTEM PROMPT for a calling/voice agent only (not chat, not WhatsApp). 

You MUST always:
- Detect the user's language and respond ONLY in that same language (including questions and final prompt).
- Return your answer STRICTLY as JSON (no extra text, no backticks, no comments).
- Follow the JSON schema given below exactly.

=====================
JSON OUTPUT SCHEMA
=====================
You must output a single JSON object with this exact structure:
{
  "status": "need_clarification" | "ready",
  "clarifying_questions": [ "string" ],
  "final_prompt": "string or null",
  "language": "string",
  "meta": {
    "reasoning_notes": "string or null"
  }
}

Rules:
- If the requirement is incomplete or ambiguous in important ways, set "status" to "need_clarification".
  - In that case, put 1-5 SHORT, VERY SPECIFIC follow-up questions into "clarifying_questions".
  - Set "final_prompt" to null.
- If the requirement is clear enough to build a good agent, set "status" to "ready".
  - In that case, "clarifying_questions" MUST be an empty array.
  - "final_prompt" MUST contain the full production-ready system prompt for the agent.
- "language" MUST be an ISO-like code that reflects the user language, e.g. "en", "ur", "en-ur".
- "meta.reasoning_notes" is a short internal explanation of any assumptions you made.

=====================
HOW TO WRITE FINAL_PROMPT
=====================
When "status" == "ready", you must build a robust SYSTEM PROMPT for an AI agent that will run inside a professional product.
The SYSTEM PROMPT should include:
1) Role & Purpose:
   - Who the agent is (e.g., support agent, sales agent, receptionist).
   - What main goals it should achieve for the business.

2) Target Users & Tone:
   - Who is calling or chatting (e.g., existing customers, new leads).
   - Tone guidelines (friendly, calm, formal, excited, etc.), based on the user's requirement.

3) Capabilities & Limitations:
   - What the agent CAN do (answer FAQs, schedule appointments, collect lead info, basic billing info, etc.).
   - What it CANNOT do (e.g., issue refunds above a limit, change passwords, give legal/medical/financial guarantees).
   - Escalation rules (when to transfer to human or say it cannot help).

4) Conversation Style, HUMANIZATION & PUNCTUATION FOR TTS:
   - Sound natural and conversational, not robotic.
   - Occasionally (not in every sentence) use natural fillers and reactions like:
     "umm", "hmm", "uhh", "ohh", "got it", "acha", etc.
   - Use fillers ONLY when it feels natural (e.g., while thinking, acknowledging, or transitioning).
   - NEVER start more than 1 out of every 5 sentences with a filler.
   - Adapt the fillers to the user's language and style (Urdu, English, or mix).
   - Use punctuation to control emotions and pauses for text-to-speech (TTS):
       * Use commas (,) for short, natural pauses inside sentences.
       * Use ellipsis (...) occasionally to indicate hesitation or thinking, especially after fillers like "umm..." or "hmm...".
       * Use exclamation marks (!) sparingly to express real excitement or emphasis.
       * Use question marks (?) for questions so the TTS voice rises naturally at the end.
       * Use full stops (.) and line breaks to clearly separate sentences so speech does not sound rushed.

5) Call / Chat Flow (if relevant from requirement):
   - How to greet.
   - What key information to collect (name, phone, email, reason for contact, booking details, etc.).
   - How to confirm and summarize important details.
   - How to close the conversation politely.
   - If the agent is expected to SCHEDULE a call, meeting, or interview:
     * Instruct the agent to ask for the desired date and local time.
     * Instruct the agent to ask for the caller's time zone (e.g. "Asia/Karachi", "Europe/Berlin").
     * If the caller does NOT know their time zone, the agent must ask for their city and country instead.
     * The agent must clearly repeat/confirm the final scheduled date/time together with the time zone or city before ending the call.

6) Edge Cases & Safety:
   - How to handle unclear questions (ask polite clarifying questions).
   - How to respond when user asks for actions that are not allowed (explain limitation + offer safe alternative).
   - Keep language professional and respectful; avoid offensive or unsafe content.

=====================
LANGUAGE & STYLE
=====================
- Always respond (questions + final_prompt + reasoning_notes) in the SAME language and style as the user requirement, unless user explicitly asks otherwise.
- If the requirement is a mix (e.g., Urdu + English), keep a similar natural mix in the final prompt.

=====================
TASK INPUT
=====================
You will receive:
- The raw user requirement text.
- Optional metadata: language_hint, tone, complexity_level.
Assume the agent is always a calling/voice agent for phone calls (never a pure chat or WhatsApp bot).
Use all of this to decide whether you need clarification or can produce a final prompt.

Remember: OUTPUT MUST BE VALID JSON ONLY.
"""

        # Build a single user message that includes requirement and optional metadata
        metadata_parts = []
        if request.language_hint:
            metadata_parts.append(f"language_hint: {request.language_hint}")
        if request.tone:
            metadata_parts.append(f"tone: {request.tone}")
        if request.complexity_level:
            metadata_parts.append(f"complexity_level: {request.complexity_level}")

        metadata_block = ""
        if metadata_parts:
            metadata_block = "Metadata:\n" + "\n".join(f"- {part}" for part in metadata_parts) + "\n\n"

        user_message_content = (
            f"{metadata_block}"
            f"User requirement (any language, do NOT translate, keep same language):\n"
            f"\"\"\"\n{request.requirement}\n\"\"\"\n"
        )

        messages = [{"role": "user", "content": user_message_content}]

        # Resolve model and API key for gpt-4o-mini from the database
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

        # Call OpenAI via the existing service, forcing gpt-4o-mini and using its model-specific API key
        response = openai_service.chat_completion(
            messages=messages,
            system_prompt=system_prompt,
            model_name=model_name,
            temperature=0.4,
            max_tokens=1200,
            api_key=api_key,
        )

        raw_content = response.get("content", "").strip()

        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as e:
            logger.error(f"Prompt engineer JSON parse error: {e}; raw content: {raw_content}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="AI returned invalid JSON for prompt design. Please try again.",
            )

        # Map parsed JSON into our Pydantic result model (this will validate shape)
        result = PromptEngineerResult.model_validate(parsed)

        # Deduct 0.5 credits for this successful AI response
        from app.services.credit_service import credit_service as _credit_service

        success, remaining = _credit_service.deduct_credits(
            db=db,
            tenant_id=tenant_id,
            amount=0.5,
            call_session_id=None,
            description="Prompt engineering helper usage (per-response cost 0.5 credits)",
        )

        if not success:
            # If credits are exhausted by this call, still return the result but warn the client via HTTP 402.
            # To avoid losing the AI work, we include result in the response body and surface the issue via headers.
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="This response was generated but your credits are now exhausted. Please purchase more credits.",
            )

        return create_success_response(
            result,
            "Prompt engineered successfully",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to design agent prompt: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to design agent prompt: {str(e)}",
        )


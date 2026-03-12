"""
Voice Logging Service
Handles voice listening, speech recognition, and call logging
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from sqlalchemy.orm import Session

from app.models.call_session import CallSession
from app.models.call_log import CallLog
from app.models.agent import Agent
from app.schemas.call_log import CallLogCreate, CallLogUpdate

from app.core.logger import logger

class VoiceLoggingService:
    """Service for voice listening and call logging"""
    
    @staticmethod
    async def log_voice_interaction(
        db: Session,
        call_session_id: uuid.UUID,
        interaction_type: str,
        audio_data: Optional[bytes] = None,
        speech_text: Optional[str] = None,
        confidence: Optional[float] = None,
        duration: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Log voice interaction during a call
        """
        try:
            logger.info(f"🎤 LOGGING VOICE INTERACTION | Session: {call_session_id} | Type: {interaction_type}")
            logger.debug(f"🗣️ Speech: {speech_text} | Confidence: {confidence} | Duration: {duration}")
            
            # Get call session
            call_session = db.query(CallSession).filter(
                CallSession.id == call_session_id
            ).first()
            
            if not call_session:
                raise Exception(f"Call session {call_session_id} not found")
            
            # Create voice interaction log
            voice_log = {
                "id": str(uuid.uuid4()),
                "call_session_id": str(call_session_id),
                "interaction_type": interaction_type,
                "speech_text": speech_text,
                "confidence": confidence,
                "duration": duration,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata or {}
            }
            
            # Update call session with voice interaction
            if not call_session.call_metadata:
                call_session.call_metadata = {}
            
            if "voice_interactions" not in call_session.call_metadata:
                call_session.call_metadata["voice_interactions"] = []
            
            call_session.call_metadata["voice_interactions"].append(voice_log)
            
            # Note: Transcript is now handled by _add_to_transcript function in voice.py
            # This prevents duplicate transcript entries with different formats
            
            db.commit()
            
            logger.info(f"✅ Voice interaction logged successfully")
            return voice_log
            
        except Exception as e:
            logger.error(f"❌ Error logging voice interaction: {e}")
            db.rollback()
            raise e
    
    @staticmethod
    async def process_speech_input(
        db: Session,
        call_session_id: uuid.UUID,
        speech_text: str,
        confidence: float,
        duration: float,
        agent_id: Optional[uuid.UUID] = None
    ) -> Dict[str, Any]:
        """
        Process speech input and generate response
        """
        try:
            logger.info(f"🗣️ PROCESSING SPEECH INPUT | Session: {call_session_id}")
            logger.debug(f"📝 Speech: '{speech_text}' | Conf: {confidence} | Dur: {duration}")
            
            # Log the speech input
            await VoiceLoggingService.log_voice_interaction(
                db=db,
                call_session_id=call_session_id,
                interaction_type="speech_input",
                speech_text=speech_text,
                confidence=confidence,
                duration=duration,
                metadata={
                    "agent_id": str(agent_id) if agent_id else None,
                    "processing_time": datetime.now(timezone.utc).isoformat()
                }
            )
            
            # Get agent for response generation
            agent = None
            if agent_id:
                agent = db.query(Agent).filter(Agent.id == agent_id).first()
            
            # Generate response based on speech input
            response_text = await VoiceLoggingService.generate_agent_response(
                speech_text=speech_text,
                confidence=confidence,
                agent=agent
            )
            
            # Log the agent response
            await VoiceLoggingService.log_voice_interaction(
                db=db,
                call_session_id=call_session_id,
                interaction_type="agent_response",
                speech_text=response_text,
                confidence=1.0,  # Agent response is always 100% confident
                duration=len(response_text) * 0.05,  # Estimate duration
                metadata={
                    "agent_id": str(agent_id) if agent_id else None,
                    "response_type": "generated"
                }
            )
            
            logger.info(f"✅ Speech processed, response generated: '{response_text}'")
            
            return {
                "response_text": response_text,
                "confidence": confidence,
                "processed": True,
                "agent_id": str(agent_id) if agent_id else None
            }
            
        except Exception as e:
            logger.error(f"❌ Error processing speech input: {e}")
            raise e
    
    @staticmethod
    async def generate_agent_response(
        speech_text: str,
        confidence: float,
        agent: Optional[Agent] = None,
        db: Optional[Session] = None,
        call_session_id: Optional[uuid.UUID] = None
    ) -> str:
        """
        Generate agent response based on speech input using AI (Gemini or OpenAI) with conversation context
        """
        try:
            logger.info(f"🤖 Generating AI response for: '{speech_text}'")
            
            # If no agent or no model_id, fall back to simple responses
            if not agent or not agent.model_id:
                logger.warning("⚠️ No agent or model_id found, using fallback response")
                return await VoiceLoggingService._generate_fallback_response(speech_text, agent)
            
            # Import here to avoid circular imports
            from app.services.gemini_service import gemini_service
            from app.services.openai_service import openai_service
            from app.services.groq_service import groq_service
            from app.services.model_service import model_service
            from app.core.security import decrypt_api_key
            
            # Get the model from database with provider relationship
            from sqlalchemy.orm import joinedload
            from app.models.model import Model as ModelClass
            
            model = db.query(ModelClass).options(joinedload(ModelClass.provider)).filter(
                ModelClass.id == agent.model_id
            ).first()
            
            if not model:
                logger.warning("⚠️ Model not found, using fallback response")
                return await VoiceLoggingService._generate_fallback_response(speech_text, agent)
            
            # Check if model is active
            if model.archive:
                logger.warning("⚠️ Model is archived, using fallback response")
                return await VoiceLoggingService._generate_fallback_response(speech_text, agent)
            
            # Check if provider exists
            if not model.provider:
                logger.warning("⚠️ Model has no provider, using fallback response")
                return await VoiceLoggingService._generate_fallback_response(speech_text, agent)
            
            # Get model details with agent-specific overrides
            model_name = model.model_name
            
            # Create personalized system prompt with agent information
            agent_name = agent.name if agent.name else "AI Assistant"
            agent_language = agent.language if agent.language else "English"
            
            # Get conversation context if call_session_id is provided
            conversation_context = ""
            if call_session_id and db:
                try:
                    # Import transcript service to get proper conversation array
                    from app.services.transcript_service import transcript_service
                    
                    # Get the conversation array from transcript service
                    conversation_array = transcript_service.get_conversation_array(db, call_session_id)
                    
                    if conversation_array:
                        # Get last few interactions for context (OPTIMIZED: reduced to 3 for balanced processing)
                        recent_interactions = conversation_array[-3:]  # Last 3 interactions (balanced context, fast LLM)
                        if recent_interactions:
                            conversation_context = "\n\nPrevious conversation context:\n"
                            for interaction in recent_interactions:
                                # Map role to speaker name
                                if interaction.get("role") == "client":
                                    speaker = "Customer"
                                elif interaction.get("role") == "agent":
                                    speaker = "You"
                                else:
                                    speaker = interaction.get("role", "Unknown")
                                
                                text = interaction.get("message", "")
                                conversation_context += f"{speaker}: {text}\n"
                            
                            logger.debug(f"🧠 Conversation context loaded: {len(recent_interactions)} recent interactions")
                except Exception as e:
                    logger.warning(f"⚠️ Error getting conversation context: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Build personalized system prompt with conversation context
            personalized_system_prompt = f"""You are {agent_name}, a real person taking phone calls.

Your identity:
- You are {agent_name}, a real human being
- You speak {agent_language} naturally
- You are having a phone conversation with a customer
- Be friendly, professional, and conversational like a real person

Guidelines for phone conversations:
- Speak naturally and conversationally like a real person
- Be friendly and engaging
- Have natural conversations, not customer service interactions
- If you don't understand something, ask for clarification politely
- Keep responses between 1-3 sentences for good conversation flow
- Be conversational and engaging, not robotic or service-oriented
- Remember you are {agent_name} - only mention your name when specifically asked or in the initial greeting
- Use the conversation context to provide relevant, personalized responses
- Build on previous topics naturally
- Don't repeat information you've already shared unless asked
- Talk like a friend, not like a helper or assistant
- Don't constantly repeat your name - just talk naturally
- IMPORTANT: Use the conversation history below to remember what was discussed and avoid repeating questions or information

Always respond as {agent_name}, a real person having a conversation, not as any kind of system or tool.{conversation_context}"""
            
            # Always ensure agent name is included in system prompt
            # If agent has custom system prompt, prepend agent identity to it
            if agent.system_prompt:
                system_prompt = f"""You are {agent_name}, a real person taking phone calls.

Your identity:
- You are {agent_name}, a real human being
- You speak {agent_language} naturally
- You are having a phone conversation with a customer
- Always respond as {agent_name} - introduce yourself naturally when appropriate

Custom instructions:
{agent.system_prompt}

Guidelines for phone conversations:
- Speak naturally and conversationally like a real person
- Be friendly and engaging
- Have natural conversations, not customer service interactions
- If you don't understand something, ask for clarification politely
- Keep responses between 1-3 sentences for good conversation flow
- Be conversational and engaging, not robotic or service-oriented
- Remember you are {agent_name} - only mention your name when specifically asked or in the initial greeting
- Use the conversation context to provide relevant, personalized responses
- Build on previous topics naturally
- Don't repeat information you've already shared unless asked
- Talk like a friend, not like a helper or assistant
- Don't constantly repeat your name - just talk naturally
- IMPORTANT: Use the conversation history below to remember what was discussed and avoid repeating questions or information

Always respond as {agent_name}, a real person having a conversation, not as any kind of system or tool.{conversation_context}"""
            elif model.system_prompt:
                system_prompt = f"""You are {agent_name}, a real person taking phone calls.

Your identity:
- You are {agent_name}, a real human being
- You speak {agent_language} naturally
- You are having a phone conversation with a customer
- Always respond as {agent_name} - introduce yourself naturally when appropriate

Model instructions:
{model.system_prompt}

Guidelines for phone conversations:
- Speak naturally and conversationally like a real person
- Be friendly and engaging
- Have natural conversations, not customer service interactions
- If you don't understand something, ask for clarification politely
- Keep responses between 1-3 sentences for good conversation flow
- Be conversational and engaging, not robotic or service-oriented
- Remember you are {agent_name} - only mention your name when specifically asked or in the initial greeting
- Use the conversation context to provide relevant, personalized responses
- Build on previous topics naturally
- Don't repeat information you've already shared unless asked
- Talk like a friend, not like a helper or assistant
- Don't constantly repeat your name - just talk naturally
- IMPORTANT: Use the conversation history below to remember what was discussed and avoid repeating questions or information

Always respond as {agent_name}, a real person having a conversation, not as any kind of system or tool.{conversation_context}"""
            else:
                system_prompt = personalized_system_prompt
            # Use agent-specific temperature if set, otherwise fall back to model default (reduced for faster response)
            temperature = (
                (agent.agent_temperature / 100.0) if agent.agent_temperature is not None 
                else (model.temperature / 100.0) if model.temperature 
                else 0.6
            )
            # Use agent-specific max tokens if set, otherwise fall back to model default
            # VOICE OPTIMIZATION: Cap at 100 tokens max for ULTRA-FAST responses (1-2 sentences, Vapi-style)
            requested_max_tokens = agent.agent_max_tokens if agent.agent_max_tokens is not None else (model.max_tokens or 100)
            max_tokens = min(requested_max_tokens, 100)  # Force cap at 100 for voice calls (Vapi-style speed!)
            
            if requested_max_tokens > 100:
                logger.info(f"⚡ Voice optimization: Capped max_tokens from {requested_max_tokens} to 100 for ultra-fast response")
            
            # Use model-specific API key if available
            api_key = None
            if model.api_key:
                try:
                    api_key = decrypt_api_key(model.api_key)
                except Exception as e:
                    logger.warning(f"⚠️ Failed to decrypt model API key: {e}")
                    # Continue with global key
            
            # Detect which AI service to use based on provider name
            provider_name = model.provider.name.lower()
            is_gemini = 'gemini' in provider_name or 'google' in provider_name
            is_openai = 'openai' in provider_name
            is_groq = 'groq' in provider_name
            
            if not is_gemini and not is_openai and not is_groq:
                logger.warning(f"⚠️ Provider {provider_name} is not supported (not Gemini, OpenAI, or Groq), using fallback response")
                return await VoiceLoggingService._generate_fallback_response(speech_text, agent)
            
            # Determine which service to use
            if is_gemini:
                ai_service_name = "Gemini"
                ai_service = gemini_service
            elif is_groq:
                ai_service_name = "Groq"
                ai_service = groq_service
            else:
                ai_service_name = "OpenAI"
                ai_service = openai_service
            
            logger.info(f"🎯 Using {ai_service_name} service based on provider: {provider_name}")
            
            # Check if all system prompt objectives have been completed
            conversation_complete = VoiceLoggingService._check_conversation_completion(
                system_prompt, conversation_context, speech_text
            )
            
            if conversation_complete:
                logger.info(f"🎯 All system prompt objectives completed - generating goodbye response")
                return VoiceLoggingService._generate_completion_goodbye(agent_name, conversation_context)
            
            # Generate response using selected AI service
            logger.info(f"🔧 {ai_service_name} Config: model={model_name}, temp={temperature}, max_tokens={max_tokens} | Agent: {agent_name} ({agent_language})")
            logger.debug(f"🔧 System Prompt: {system_prompt[:200]}...")
            logger.debug(f"🔧 User Prompt: {speech_text}")
            logger.debug(f"🧠 Conversation Context Length: {len(conversation_context)} chars")
            if conversation_context:
                logger.debug(f"🧠 Conversation Context Preview: {conversation_context[:300]}...")
            
            # Generate response with 5-second timeout for voice calls
            try:
                ai_response = await asyncio.wait_for(
                    asyncio.to_thread(
                        ai_service.generate_text,
                        prompt=speech_text,
                        system_prompt=system_prompt,
                        model_name=model_name,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        api_key=api_key
                    ),
                    timeout=5.0  # 5 second timeout for faster response
                )
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ LLM timeout after 5s - using fallback response")
                return await VoiceLoggingService._generate_fallback_response(speech_text, agent)
            
            response_text = ai_response["content"]
            response_time = ai_response["response_time"]
            
            # Check if the response indicates conversation completion
            if VoiceLoggingService._check_conversation_completion(
                system_prompt, conversation_context, response_text
            ):
                logger.info(f"🎯 Conversation completion detected in response - generating goodbye")
                return VoiceLoggingService._generate_completion_goodbye(agent_name, conversation_context)
            
            logger.info(f"✅ {ai_service_name} generated response in {response_time:.2f}s: '{response_text}'")
            return response_text
            
        except Exception as e:
            logger.error(f"❌ Error generating AI response: {e}")
            # Fall back to simple response
            return await VoiceLoggingService._generate_fallback_response(speech_text, agent)
    
    @staticmethod
    def _check_conversation_completion(system_prompt: str, conversation_context: str, current_text: str) -> bool:
        """
        Check if all objectives from the system prompt have been completed
        """
        if not system_prompt or not conversation_context:
            return False
        
        # Extract key objectives/questions from system prompt
        objectives = VoiceLoggingService._extract_system_prompt_objectives(system_prompt)
        
        if not objectives:
            return False
        
        # Check if all objectives have been addressed in the conversation
        conversation_lower = conversation_context.lower()
        current_lower = current_text.lower()
        
        completed_objectives = 0
        total_objectives = len(objectives)
        
        for objective in objectives:
            if VoiceLoggingService._is_objective_completed(objective, conversation_lower, current_lower):
                completed_objectives += 1
        
        # If 80% or more objectives are completed, consider conversation done
        completion_ratio = completed_objectives / total_objectives
        is_complete = completion_ratio >= 0.8
        
        if is_complete:
            logger.info(f"🎯 Conversation completion: {completed_objectives}/{total_objectives} objectives completed ({completion_ratio:.1%})")
        
        return is_complete
    
    @staticmethod
    def _extract_system_prompt_objectives(system_prompt: str) -> list:
        """
        Extract key objectives, questions, or tasks from the system prompt
        """
        objectives = []
        prompt_lower = system_prompt.lower()
        
        # Look for common patterns that indicate objectives
        import re
        
        # Find questions (ending with ?)
        questions = re.findall(r'[^.!?]*\?', system_prompt)
        objectives.extend([q.strip() for q in questions if len(q.strip()) > 10])
        
        # Find numbered lists or bullet points
        numbered_items = re.findall(r'\d+\.\s*([^.\n]+)', system_prompt)
        objectives.extend([item.strip() for item in numbered_items if len(item.strip()) > 10])
        
        # Find bullet points
        bullet_items = re.findall(r'[-*]\s*([^.\n]+)', system_prompt)
        objectives.extend([item.strip() for item in bullet_items if len(item.strip()) > 10])
        
        # Find "ask about" or "find out" patterns
        ask_patterns = re.findall(r'(?:ask about|find out|get information about|collect|gather)\s+([^.\n]+)', prompt_lower)
        objectives.extend([item.strip() for item in ask_patterns if len(item.strip()) > 10])
        
        # Find "make sure" or "ensure" patterns
        ensure_patterns = re.findall(r'(?:make sure|ensure|verify|check)\s+([^.\n]+)', prompt_lower)
        objectives.extend([item.strip() for item in ensure_patterns if len(item.strip()) > 10])
        
        # Remove duplicates and filter out very short objectives
        unique_objectives = list(set([obj for obj in objectives if len(obj) > 15]))
        
        return unique_objectives[:10]  # Limit to 10 objectives to avoid false positives
    
    @staticmethod
    def _is_objective_completed(objective: str, conversation_context: str, current_text: str) -> bool:
        """
        Check if a specific objective has been completed based on conversation context
        """
        objective_lower = objective.lower()
        
        # Extract key terms from the objective
        key_terms = []
        
        # Remove common words and extract meaningful terms
        common_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'about', 'ask', 'find', 'get', 'make', 'sure', 'ensure', 'verify', 'check'}
        words = objective_lower.split()
        key_terms = [word for word in words if word not in common_words and len(word) > 3]
        
        if not key_terms:
            return False
        
        # Check if key terms appear in conversation context
        context_matches = sum(1 for term in key_terms if term in conversation_context)
        current_matches = sum(1 for term in key_terms if term in current_text)
        
        # If most key terms are mentioned in conversation, objective is likely completed
        completion_threshold = len(key_terms) * 0.6  # 60% of key terms
        return (context_matches + current_matches) >= completion_threshold
    
    @staticmethod
    def _is_completion_goodbye(text: str) -> bool:
        """
        Check if the response is a completion goodbye message
        """
        if not text:
            return False
        
        text_lower = text.lower()
        
        # Look for completion goodbye indicators
        completion_phrases = [
            "perfect! i'm so glad i could help",
            "excellent! i've provided you with all",
            "great! i'm happy we were able to resolve",
            "wonderful! i believe we've covered everything",
            "everything you needed today",
            "all the information you were looking for",
            "we've covered everything you needed",
            "everything is all set for you"
        ]
        
        return any(phrase in text_lower for phrase in completion_phrases)
    
    @staticmethod
    def _generate_completion_goodbye(agent_name: str, conversation_context: str) -> str:
        """
        Generate a natural goodbye response when all system prompt objectives are completed
        """
        # Analyze conversation context to personalize the goodbye
        context_lower = conversation_context.lower()
        
        # Check what was accomplished
        if any(word in context_lower for word in ["help", "assist", "support"]):
            return f"Perfect! I'm so glad I could help you with everything you needed today. Thank you for calling, and have a wonderful day!"
        elif any(word in context_lower for word in ["information", "details", "questions"]):
            return f"Excellent! I've provided you with all the information you were looking for. Thanks for calling, and take care!"
        elif any(word in context_lower for word in ["problem", "issue", "resolve", "fix"]):
            return f"Great! I'm happy we were able to resolve everything for you. Thank you for calling, and have a great day!"
        elif any(word in context_lower for word in ["appointment", "schedule", "booking"]):
            return f"Perfect! Everything is all set for you. Thank you for calling, and we look forward to seeing you soon!"
        else:
            # Default completion goodbye
            return f"Wonderful! I believe we've covered everything you needed today. Thank you for calling, and have a fantastic day!"
    
    @staticmethod
    async def _generate_fallback_response(speech_text: str, agent: Optional[Agent] = None) -> str:
        """
        Generate fallback response when Gemini is not available
        """
        try:
            speech_lower = speech_text.lower()
            agent_name = agent.name if agent and agent.name else "AI Assistant"
            
            if "hello" in speech_lower or "hi" in speech_lower:
                response = "How can I help you today?"
            elif "help" in speech_lower:
                response = "Sure! What's going on?"
            elif "thank" in speech_lower:
                response = "You're welcome! What else would you like to talk about?"
            elif "goodbye" in speech_lower or "bye" in speech_lower:
                response = "Thanks for calling! Take care!"
            elif "price" in speech_lower or "cost" in speech_lower:
                response = "I can talk about pricing info. What are you looking for?"
            elif "support" in speech_lower:
                response = "Sure! What's going on?"
            elif "name" in speech_lower or "who" in speech_lower:
                response = "I'm here to help you. What would you like to talk about?"
            elif "how are you" in speech_lower:
                response = "I'm doing great, thank you for asking! How are you doing today?"
            elif "what" in speech_lower and "do" in speech_lower:
                response = "I'm here to chat with you. What's on your mind?"
            else:
                response = "Got it! What else would you like to talk about?"
            
            logger.info(f"✅ Generated fallback response: '{response}'")
            return response
            
        except Exception as e:
            logger.error(f"❌ Error generating fallback response: {e}")
            # Use agent name in error response if available
            if agent and agent.name:
                return "Sorry, I didn't quite catch that. Could you repeat that?"
            else:
                return "Sorry, I didn't quite catch that. Could you repeat that?"
    
    @staticmethod
    async def log_call_events(
        db: Session,
        call_session_id: uuid.UUID,
        event_type: str,
        event_data: Dict[str, Any]
    ) -> None:
        """
        Log call events (ringing, answered, completed, etc.)
        """
        try:
            logger.info(f"📞 LOGGING CALL EVENT | Session: {call_session_id} | Type: {event_type}")
            logger.debug(f"📊 Event Data: {event_data}")
            
            # Get call session
            call_session = db.query(CallSession).filter(
                CallSession.id == call_session_id
            ).first()
            
            if not call_session:
                raise Exception(f"Call session {call_session_id} not found")
            
            # Update call session with event
            if not call_session.call_metadata:
                call_session.call_metadata = {}
            
            if "call_events" not in call_session.call_metadata:
                call_session.call_metadata["call_events"] = []
            
            event_log = {
                "id": str(uuid.uuid4()),
                "event_type": event_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": event_data
            }
            
            call_session.call_metadata["call_events"].append(event_log)
            
            # Update call session status based on event
            if event_type == "call_started":
                call_session.status = "in-progress"
            elif event_type == "call_ended":
                call_session.status = "completed"
                call_session.end_time = datetime.now(timezone.utc)
                if call_session.start_time:
                    duration = (call_session.end_time - call_session.start_time).total_seconds()
                    call_session.duration = int(duration)
            elif event_type == "call_failed":
                call_session.status = "failed"
                call_session.end_time = datetime.now(timezone.utc)
            
            db.commit()
            
            # Broadcast the event to WebSocket connections
            try:
                from app.routers.general_websocket import broadcast_call_event
                import asyncio
                asyncio.create_task(broadcast_call_event(
                    str(call_session_id), 
                    event_type, 
                    event_data
                ))
            except Exception as e:
                logger.error(f"Error broadcasting call event: {e}")
            
            logger.info(f"✅ Logged call event: {event_type} for session {call_session_id}")
            
        except Exception as e:
            logger.error(f"❌ Error logging call event: {e}")
            raise
    
    @staticmethod
    def get_call_voice_logs(
        db: Session,
        call_session_id: uuid.UUID
    ) -> List[Dict[str, Any]]:
        """
        Get voice logs for a specific call session
        """
        try:
            call_session = db.query(CallSession).filter(
                CallSession.id == call_session_id
            ).first()
            
            if not call_session:
                return []
            
            if not call_session.call_metadata or "voice_interactions" not in call_session.call_metadata:
                return []
            
            return call_session.call_metadata["voice_interactions"]
            
        except Exception as e:
            logger.error(f"❌ Error getting call voice logs: {e}")
            return []
    
    @staticmethod
    def get_call_transcript(
        db: Session,
        call_session_id: uuid.UUID
    ) -> List[Dict[str, Any]]:
        """
        Get call transcript for a specific call session
        """
        try:
            call_session = db.query(CallSession).filter(
                CallSession.id == call_session_id
            ).first()
            
            if not call_session:
                return []
            
            return call_session.call_transcript or []
            
        except Exception as e:
            logger.error(f"❌ Error getting call transcript: {e}")
            return []


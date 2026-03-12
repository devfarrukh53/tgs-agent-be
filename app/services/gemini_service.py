"""
Gemini Service Module
Handles all Gemini-related operations including text generation
"""

from google import genai
from app.core.config import settings
from typing import List, Dict, Optional, Any
import time
import json

class GeminiService:
    """Service class for handling Gemini operations"""
    
    def __init__(self):
        self._client = None
        self._model_cache = {}
        self._current_api_key = None
    
    def _extract_text_from_response(self, response):
        """Helper method to extract text from response (handles different API structures)"""
        if hasattr(response, 'text') and response.text:
            return response.text
        elif hasattr(response, 'candidates') and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, 'content') and candidate.content:
                if hasattr(candidate.content, 'parts') and candidate.content.parts:
                    return candidate.content.parts[0].text if hasattr(candidate.content.parts[0], 'text') else ""
        elif hasattr(response, 'content') and response.content:
            if hasattr(response.content, 'parts') and response.content.parts:
                return response.content.parts[0].text if hasattr(response.content.parts[0], 'text') else ""
        return ""
    
    def get_client(self, api_key: str = None):
        """Get or create Gemini client with specific API key using google-genai SDK."""
        # Use provided API key or fall back to global setting
        key_to_use = api_key or settings.GEMINI_API_KEY
        
        if not key_to_use:
            raise Exception(
                "Gemini API key not found. Please provide an API key or set GEMINI_API_KEY in your config."
            )
        
        # Only recreate client if the API key has changed or client is missing
        if self._current_api_key != key_to_use or self._client is None:
            self._client = genai.Client(api_key=key_to_use)
            self._current_api_key = key_to_use
            self._model_cache = {}  # Kept for backward compatibility, not used with new SDK
        
        return self._client
    
    def generate_text(self, prompt: str, system_prompt: str = None, 
                     model_name: str = "gemini-1.5-flash", 
                     temperature: float = 0.7, 
                     max_tokens: int = 1000,
                     api_key: str = None) -> Dict[str, Any]:
        """
        Generate text using Gemini API
        
        Args:
            prompt: The input prompt for text generation
            system_prompt: System prompt to set the context
            model_name: Gemini model to use
            temperature: Temperature setting (0.0 to 1.0)
            max_tokens: Maximum tokens for response
            api_key: Model-specific API key (optional)
            
        Returns:
            Dictionary with response content and metadata
        """
        try:
            start_time = time.time()
            
            # Get client instance with specific API key
            client = self.get_client(api_key)
            
            # Prepare the full prompt
            full_prompt = prompt
            if system_prompt:
                full_prompt = f"System: {system_prompt}\n\nUser: {prompt}"
            
            # Generate content using google-genai Client
            response = client.models.generate_content(
                model=model_name,
                contents=full_prompt,
                config={
                    "temperature": float(temperature),
                    "max_output_tokens": int(max_tokens),
                },
            )
            
            end_time = time.time()
            response_time = end_time - start_time
            
            response_text = self._extract_text_from_response(response)
            
            return {
                "content": response_text,
                "model": model_name,
                "response_time": response_time,
                "usage": {
                    "prompt_tokens": len(full_prompt.split()),  # Approximate
                    "completion_tokens": len(response_text.split()),  # Approximate
                    "total_tokens": len(full_prompt.split()) + len(response_text.split())
                },
                "finish_reason": "stop"  # Gemini doesn't provide this directly
            }
            
        except Exception as e:
            raise Exception(f"Error in Gemini text generation: {str(e)}")

    async def stream_text(self, prompt: str, system_prompt: str = None,
                          model_name: str = "gemini-1.5-flash",
                          temperature: float = 0.7,
                          max_tokens: int = 1000,
                          api_key: str = None):
        """Yield text chunks from Gemini as they arrive (streaming).
        This returns an async iterator of strings.
        """
        import asyncio
        import threading
        from queue import Queue

        # Prepare prompt
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"System: {system_prompt}\n\nUser: {prompt}"

        # Get client instance (thread-safe via global client)
        client = self.get_client(api_key)

        q: Queue = Queue()
        SENTINEL = object()

        def producer():
            try:
                response = client.models.generate_content_stream(
                    model=model_name,
                    contents=full_prompt,
                    config={
                        "temperature": float(temperature),
                        "max_output_tokens": int(max_tokens),
                    },
                )
                for chunk in response:
                    try:
                        text = self._extract_text_from_response(chunk)
                        if text:
                            q.put(text)
                    except Exception:
                        # Skip malformed events
                        continue
            except Exception as e:
                q.put(("__error__", str(e)))
            finally:
                q.put(SENTINEL)

        threading.Thread(target=producer, daemon=True).start()

        loop = asyncio.get_event_loop()

        while True:
            chunk = await loop.run_in_executor(None, q.get)
            if isinstance(chunk, tuple) and len(chunk) == 2 and chunk[0] == "__error__":
                # Raise so caller can handle fallback logic instead of speaking error text
                raise Exception(chunk[1])
            if chunk is SENTINEL:
                break
            yield str(chunk)
    
    def chat_completion(self, messages: List[Dict[str, str]], 
                       system_prompt: str = None, 
                       model_name: str = "gemini-1.5-flash", 
                       temperature: float = 0.7,
                       max_tokens: int = 1000,
                       api_key: str = None) -> Dict[str, Any]:
        """
        Generate chat completion using Gemini API
        
        Args:
            messages: List of message dictionaries with 'role' and 'content'
            system_prompt: System prompt to use for the conversation
            model_name: Gemini model to use
            temperature: Temperature setting (0.0 to 1.0)
            max_tokens: Maximum tokens for response
            api_key: Model-specific API key (optional)
            
        Returns:
            Dictionary with response content and metadata
        """
        try:
            start_time = time.time()
            
            # Get client instance with specific API key
            client = self.get_client(api_key)
            
            # Build a simple formatted conversation for text-only models
            lines = []
            if system_prompt:
                lines.append(f"System: {system_prompt}")
            
            for message in messages:
                role = message.get("role", "user")
                content = message.get("content", "")
                if role == "assistant":
                    prefix = "Assistant"
                elif role == "system":
                    prefix = "System"
                else:
                    prefix = "User"
                lines.append(f"{prefix}: {content}")
            
            formatted_prompt = "\n\n".join(lines)
            
            response = client.models.generate_content(
                model=model_name,
                contents=formatted_prompt,
                config={
                    "temperature": float(temperature),
                    "max_output_tokens": int(max_tokens),
                },
            )
            
            end_time = time.time()
            response_time = end_time - start_time
            
            response_text = self._extract_text_from_response(response)
            
            return {
                "content": response_text,
                "model": model_name,
                "response_time": response_time,
                "usage": {
                    "prompt_tokens": len(" ".join([msg["content"] for msg in messages]).split()),
                    "completion_tokens": len(response_text.split()),
                    "total_tokens": len(" ".join([msg["content"] for msg in messages]).split()) + len(response_text.split())
                },
                "finish_reason": "stop"
            }
            
        except Exception as e:
            raise Exception(f"Error in Gemini chat completion: {str(e)}")
    
    def process_agent_conversation(self, user_input: str, 
                             agent_system_prompt: str = "You are a helpful assistant.",
                             call_session=None,
                             db=None,
                             model_name: str = "gemini-1.5-flash",
                             temperature: float = 0.7,
                             max_tokens: int = 1000,
                             api_key: str = None) -> Dict[str, Any]:
        """
        Process agent conversation using Gemini API with DB context memory.
        """
        try:
            from datetime import datetime
            import json

            start_time = time.time()

            # 🧠 Get model instance
            model = self.get_model(model_name, api_key)

            # 📜 Load previous transcript (if any)
            conversation_history = []
            if call_session and call_session.call_transcript:
                try:
                    conversation_history = json.loads(call_session.call_transcript)
                except:
                    conversation_history = []

            # 🗣️ Prepare message context
            history_lines = []
            for msg in conversation_history[-6:]:
                if isinstance(msg, dict):
                    role = msg.get('role', 'user')
                    content = msg.get('content') or msg.get('message', '')
                    if content:
                        history_lines.append(f"{role.capitalize()}: {content}")
            history_text = "\n".join(history_lines)

            full_prompt = (
                f"System: {agent_system_prompt}\n\n"
                f"Previous conversation:\n{history_text}\n\n"
                f"User: {user_input}\n\n"
                f"Note: Never repeat any question already asked. Be human-like and polite."
            )

            # 💬 Generate model response
            response = model.generate_content(
                contents=full_prompt,
                config={
                    "temperature": temperature,
                    "max_output_tokens": max_tokens,
                }
            )

            # ⏱️ Timing
            end_time = time.time()
            response_time = end_time - start_time

            # 🪄 Update transcript in DB (use "message" key for consistency)
            response_text = self._extract_text_from_response(response)
            
            if call_session and db:
                conversation_history.append({"role": "user", "message": user_input})
                conversation_history.append({"role": "assistant", "message": response_text})
                call_session.call_transcript = json.dumps(conversation_history)
                call_session.updated_at = datetime.utcnow()
                db.commit()

            return {
                "response": response_text,
                "model": model_name,
                "response_time": response_time,
                "usage": {
                    "prompt_tokens": len(full_prompt.split()),
                    "completion_tokens": len(response_text.split()),
                    "total_tokens": len(full_prompt.split()) + len(response_text.split())
                }
            }

        except Exception as e:
            raise Exception(f"Error in Gemini agent conversation: {str(e)}")
        
# Create a singleton instance
gemini_service = GeminiService()

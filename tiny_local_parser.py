import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Qwen2.5-0.5B is ultra-lightweight (only ~398 MB in Q4_K_M GGUF format)
REPO_ID = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
FILENAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", FILENAME)

# Lazy loaded llama instance
_llm = None

SYSTEM_PROMPT = """You are a precise JSON extraction engine. Extract the required fields from the job posting below. Output ONLY valid JSON matching exactly this schema:
{
  "role": "Job title or null",
  "client": "Company name or null",
  "location": "Location or null",
  "rate": "Rate or null",
  "duration": "Duration or null",
  "work_mode": "REMOTE, HYBRID, ONSITE, or UNKNOWN",
  "employment_types": ["C2C", "W2", "CONTRACT", "FULLTIME"],
  "experience": "Experience required or null",
  "skills": ["Skill1", "Skill2"]
}
"""

def _get_llm():
    global _llm
    if _llm is None:
        try:
            from huggingface_hub import hf_hub_download
            from llama_cpp import Llama
            
            os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
            
            if not os.path.exists(MODEL_PATH):
                logger.info(f"Downloading {FILENAME} (~398 MB) for local offline parsing...")
                hf_hub_download(
                    repo_id=REPO_ID,
                    filename=FILENAME,
                    local_dir=os.path.dirname(MODEL_PATH)
                )
            
            logger.info("Loading tiny GGUF model into RAM...")
            _llm = Llama(
                model_path=MODEL_PATH,
                n_ctx=4096,          # Increased context window to prevent token errors
                n_threads=2,         # Fewer threads to prevent CPU spikes
                verbose=False
            )
        except ImportError:
            logger.warning("llama-cpp-python or huggingface-hub not installed. Tiny Local LLM will fail.")
            raise
    return _llm

def parse_requirement_tiny(subject: str, body: str) -> Optional[dict]:
    try:
        llm = _get_llm()
    except Exception as e:
        logger.warning(f"Failed to load Tiny LLM: {e}")
        return None

    try:
        user_prompt = f"SUBJECT: {subject}\n\nBODY: {body}"
        
        response = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            response_format={
                "type": "json_object",
            },
            max_tokens=1500,
            temperature=0.1
        )
        
        content = response["choices"][0]["message"]["content"].strip()
            
        parsed = json.loads(content)
        parsed["parsing_model"] = f"Tiny Local LLM (Qwen 0.5B)"
        
        if not parsed.get("work_mode"):
            parsed["work_mode"] = "UNKNOWN"
        if not parsed.get("employment_types"):
            parsed["employment_types"] = ["UNKNOWN"]
            
        return parsed
            
    except Exception as e:
        logger.warning(f"Error executing Tiny Local LLM parser: {e}")
        
    return None

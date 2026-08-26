import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Qwen2.5-1.5B is highly capable and tiny (only ~1.1 GB in Q4_K_M GGUF format)
REPO_ID = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
FILENAME = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
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
                logger.info(f"Downloading {FILENAME} (~1.1 GB) for local offline parsing...")
                # Download model to our models/ directory automatically
                hf_hub_download(
                    repo_id=REPO_ID,
                    filename=FILENAME,
                    local_dir=os.path.dirname(MODEL_PATH)
                )
            
            logger.info("Loading GGUF model into RAM...")
            _llm = Llama(
                model_path=MODEL_PATH,
                n_ctx=2048,          # Context window
                n_threads=4,         # CPU threads
                verbose=False        # Keep logs clean
            )
        except ImportError:
            logger.warning("llama-cpp-python or huggingface-hub not installed. Local LLM will fail.")
            raise
    return _llm

def parse_requirement_local(subject: str, body: str) -> Optional[dict]:
    try:
        llm = _get_llm()
    except Exception as e:
        logger.warning(f"Failed to load local LLM: {e}")
        return None

    try:
        user_prompt = f"SUBJECT: {subject}\n\nBODY: {body}"
        
        # We format it in ChatML for Qwen
        prompt = f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n<|im_start|>user\n{user_prompt}<|im_end|>\n<|im_start|>assistant\n"
        
        response = llm(
            prompt,
            max_tokens=800,
            temperature=0.1,
            stop=["<|im_end|>"]
        )
        
        content = response["choices"][0]["text"].strip()
        
        # Sometimes models wrap output in markdown JSON fences
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
            
        parsed = json.loads(content)
        parsed["parsing_model"] = f"Local CPU (Qwen2.5-1.5B)"
        
        if not parsed.get("work_mode"):
            parsed["work_mode"] = "UNKNOWN"
        if not parsed.get("employment_types"):
            parsed["employment_types"] = ["UNKNOWN"]
            
        return parsed
            
    except Exception as e:
        logger.warning(f"Error executing Local LLM parser: {e}")
        
    return None

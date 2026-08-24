import json
import os
import threading
from typing import Any, Dict, Optional

# Define the cache directory path
CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "cache")

class PersistentDiskCache:
    def __init__(self, filename: str):
        self.filename = filename
        self.filepath = os.path.join(CACHE_DIR, filename)
        self.lock = threading.Lock()
        self.cache: Dict[str, Any] = self._load()

    def _ensure_dir(self):
        if not os.path.exists(CACHE_DIR):
            os.makedirs(CACHE_DIR, exist_ok=True)

    def _load(self) -> Dict[str, Any]:
        self._ensure_dir()
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Failed to load cache from {self.filepath}: {e}")
                return {}
        return {}

    def _save(self) -> None:
        self._ensure_dir()
        # Write to a temporary file first, then rename for atomic save
        temp_filepath = self.filepath + ".tmp"
        try:
            with open(temp_filepath, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, indent=2)
            os.replace(temp_filepath, self.filepath)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to save cache to {self.filepath}: {e}")

    def get(self, key: str) -> Optional[Any]:
        with self.lock:
            return self.cache.get(key)

    def set(self, key: str, value: Any) -> None:
        with self.lock:
            self.cache[key] = value
            self._save()

    def has(self, key: str) -> bool:
        with self.lock:
            return key in self.cache

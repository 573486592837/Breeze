import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Settings:
    def __init__(self) -> None:
        self.api_key: str = os.getenv("SILICONFLOW_API_KEY", "")
        self.model: str = os.getenv("BREEZE_MODEL", "openai/Pro/deepseek-ai/DeepSeek-V3.2")
        self.api_base: str = os.getenv("BREEZE_API_BASE", "https://api.siliconflow.cn/v1")
        self.repo_path: Path = Path.cwd()

        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY is not set in the environment variables.")


settings = Settings()

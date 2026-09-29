"""Day 1 验证脚本：确认 LLM 单轮调用可用。"""

from breeze.llm import LLMClient


def main() -> None:
    client = LLMClient()
    resp = client.chat(
        messages=[
            {"role": "system", "content": "你是 Breeze，一个代码仓库 Agent。"},
            {"role": "user", "content": "用一句话解释什么是 Agent Loop。"},
        ]
    )
    print("=== 内容 ===")
    print(resp.content)
    print("\n=== Token 用量 ===")
    print(f"prompt: {resp.prompt_tokens}")
    print(f"completion: {resp.completion_tokens}")
    print(f"total: {resp.total_tokens}")


if __name__ == "__main__":
    main()

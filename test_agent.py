import asyncio
import json
import os

from openai import AsyncOpenAI
from openreward import AsyncOpenReward

MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-5.4")
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENREWARD_API_KEY = os.environ.get("OPENREWARD_API_KEY", "dummy")


async def main() -> None:
    # Connect to local server
    or_client = AsyncOpenReward()
    oai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    # Get environment
    environment = or_client.environments.get(name="GeneralReasoning/SETA", base_url="http://localhost:8080")

    # List tasks
    tasks = await environment.list_tasks(split="train")
    print(f"Found {len(tasks)} tasks")

    # Get tools (CLI tools + submit_solution)
    tools = await environment.list_tools(format="openai")

    # Test first task (or specify task_id)
    # You can filter by task_id: [t for t in tasks if t.task_spec["task_id"] == 5][0]
    task = tasks[305]
    print(f"\nTesting Task {task.task_spec['task_id']}")
    print(f"Category: {task.task_spec['category']}")
    print(f"Difficulty: {task.task_spec['difficulty']}")

    # Create session
    async with environment.session(
        task=task,
        secrets={
            "openreward_api_key": OPENREWARD_API_KEY,
            "openai_api_key": OPENAI_API_KEY,
        }
    ) as session:
        # Get initial prompt
        prompt = await session.get_prompt()
        # prompt is List[TextBlock], extract the text
        prompt_text = prompt[0].text if isinstance(prompt, list) else prompt
        input_list = [{"role": "user", "content": prompt_text}]
        print(input_list[-1])

        finished = False
        turn = 0
        max_turns = 50  # Safety limit

        while not finished and turn < max_turns:
            turn += 1
            print(f"\n--- Turn {turn} ---")

            # Get model response
            response = await oai_client.responses.create(
                model=MODEL_NAME,
                #reasoning={"effort": "high"},
                tools=tools,
                input=input_list,
            )
            print(response.output)

            # Process output
            for item in response.output:
                input_list.append(item)

                # Handle tool calls
                if item.type == "function_call":
                    print(f"Tool call: {item.name}")

                    # Execute tool in session
                    tool_result = await session.call_tool(
                        item.name,
                        json.loads(str(item.arguments)),
                    )

                    # Add tool result to input
                    input_list.append({
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": tool_result.blocks[0].text if tool_result.blocks else "",
                    })
                    print(input_list[-1])

                    # Check if finished
                    if tool_result.finished:
                        finished = True
                        print(f"\n=== FINISHED ===")
                        print(f"Final Score: {tool_result.reward:.2%}")
                        print(f"Metadata: {tool_result.metadata}")
                        break

                # Handle text output
                elif item.type == "text":
                    print(f"Model: {item.text[:100]}...")

            # Safety check
            if not any(i.type == "function_call" for i in response.output):
                print("No tool call, model may be stuck")
                break

        if not finished:
            print(f"\nTask not completed after {turn} turns")


if __name__ == "__main__":
    asyncio.run(main())

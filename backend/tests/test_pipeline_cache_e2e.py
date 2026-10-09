import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    EndFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask
from pipecat.pipeline.runner import PipelineRunner
from helpers.receiver_cache import ReceiverResponseCache, ReceiverResponseCacheProcessor

class MockLLM(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.call_count = 0
        self.last_prompt = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self.call_count += 1
            self.last_prompt = [dict(m) for m in frame.context.messages]
            # Emit mock reply
            from pipecat.frames.frames import LLMFullResponseStartFrame, TextFrame, LLMFullResponseEndFrame
            await self.push_frame(LLMFullResponseStartFrame())
            await self.push_frame(TextFrame("LLM generated reply"))
            await self.push_frame(LLMFullResponseEndFrame())
        else:
            await self.push_frame(frame, direction)

async def run_test():
    ctx = {
        "customer_name": "Test Corp",
        "service_name": "Broadband",
        "amount": "20,000",
        "billing_period": "July 2026",
        "due_date": "10 July 2026",
        "invoice_number": "INV-100",
        "call_type": "overdue",
    }
    llm_context = LLMContext([
        {"role": "system", "content": "You are Arjun"},
        {"role": "assistant", "content": "Hi, English or Hindi?"}
    ])
    user_agg, asst_agg = LLMContextAggregatorPair(llm_context, user_params=LLMUserAggregatorParams(user_turn_stop_timeout=0.1))
    cache = ReceiverResponseCache(ctx)
    cache_proc = ReceiverResponseCacheProcessor(cache, ref_id="test-123")
    mock_llm = MockLLM()

    pipeline = Pipeline([
        user_agg,
        cache_proc,
        mock_llm,
        asst_agg,
    ])
    task = PipelineTask(pipeline)
    runner = PipelineRunner(handle_sigint=False)

    async def simulate_conversation():
        await asyncio.sleep(0.1)

        # TURN 1: User says "English please"
        print("--- SIMULATING TURN 1: 'English please' ---")
        llm_context.add_message({"role": "user", "content": "English please"})
        await user_agg.push_context_frame()
        await asyncio.sleep(0.3)

        assert mock_llm.call_count == 0, f"Expected LLM calls = 0, got {mock_llm.call_count}"
        print("TURN 1 OK: LLM call count is 0 (Cache successfully intercepted!)")
        print("Last assistant message in context:", llm_context.messages[-1]["content"][:60], "...")

        # TURN 2: User says "I will pay next Monday"
        print("\n--- SIMULATING TURN 2: 'I will pay next Monday' ---")
        llm_context.add_message({"role": "user", "content": "I will pay next Monday"})
        await user_agg.push_context_frame()
        await asyncio.sleep(0.3)

        assert mock_llm.call_count == 1, f"Expected LLM calls = 1, got {mock_llm.call_count}"
        print("TURN 2 OK: LLM call count is 1 (LLM was invoked for custom date promise!)")
        print("Last assistant message in context:", llm_context.messages[-1]["content"])

        await task.queue_frames([EndFrame()])

    asyncio.create_task(simulate_conversation())
    await runner.run(task)

    print("\n--- FINAL CONVERSATION TRANSCRIPT ---")
    for m in llm_context.messages:
        print(f"[{m['role'].upper()}]: {m['content']}")

    print("\nEND-TO-END PIPELINE SIMULATION PASSED 100%!")

if __name__ == "__main__":
    asyncio.run(run_test())

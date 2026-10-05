import time, requests

def timed_call(label, prompt, n=3):
    times = []
    for i in range(n):
        start = time.time()
        resp = requests.post("http://localhost:11434/api/generate", json={
            "model": "llama3.2:3b", "prompt": prompt, "stream": False, "options": {"num_predict": 60}
        })
        elapsed = time.time() - start
        times.append(elapsed)
        print(f"{label} run {i+1}: {elapsed:.2f}s -> {resp.json().get('response')!r}")
    print(f"{label} average: {sum(times)/len(times):.2f}s\n")

prompt_single = "What's the RSI verdict for this reading: 45.2? Respond with exactly one word: buy, sell, or hold."
prompt_combined = """Given these four indicator readings, respond with exactly 4 lines in the format "INDICATOR: verdict", nothing else:
RSI: 45.2
Stoch RSI: 38.1
Bollinger %B: 0.42
ADX: 17.3 (+DI 22, -DI 19)"""

timed_call("single", prompt_single)
timed_call("combined", prompt_combined)
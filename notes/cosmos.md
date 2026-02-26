# Test Cosmos 2B

## Check it's running
curl -s http://localhost:8000/v1/models | python3 -m json.tool

### Test query
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/Cosmos-Reason2-2B",
    "messages": [
      {"role": "system", "content": "You are a robot task planner for a UR5e arm with a Robotiq 2F-85 gripper."},
      {"role": "user", "content": "The robot needs to pick an object at x=0.4, y=0.1, z=0.05. Plan the actions."}
    ],
    "temperature": 0.3,
    "max_tokens": 1024
  }' | python3 -m json.tool


# Switch to Cosmos 8B

### Kill the 2B server
pkill -f "vllm serve"

# Start the 8B server
export PATH="$HOME/.local/bin:$PATH"
vllm serve nvidia/Cosmos-Reason2-8B \
  --max-model-len 16384 \
  --reasoning-parser qwen3 \
  --port 8000 \
  --gpu-memory-utilization 0.85

### (wait until you see "Uvicorn running on http://0.0.0.0:8000")
Then in another terminal:


## Test 8B query
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/Cosmos-Reason2-8B",
    "messages": [
      {"role": "system", "content": "You are a robot task planner for a UR5e arm with a Robotiq 2F-85 gripper."},
      {"role": "user", "content": "The robot needs to pick an object at x=0.4, y=0.1, z=0.05. Plan the actions."}
    ],
    "temperature": 0.3,
    "max_tokens": 1024
  }' | python3 -m json.tool


## Switch back to 2B

pkill -f "vllm serve"
export PATH="$HOME/.local/bin:$PATH"
vllm serve nvidia/Cosmos-Reason2-2B \
  --max-model-len 16384 \
  --reasoning-parser qwen3 \
  --port 8000 \
  --gpu-memory-utilization 0.85




## These are commands from scratch (assuming nothing is running). 
Here's the distinction:

If 2B is already running (your current state)
Just test it directly:


curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/Cosmos-Reason2-2B",
    "messages": [
      {"role": "system", "content": "You are a robot task planner for a UR5e
       arm with a Robotiq 2F-85 gripper."},
      {"role": "user", "content": "The robot needs to pick an object 
      at x=0.4, y=0.1, z=0.05. Plan the actions."}
    ],
    "temperature": 0.3,
    "max_tokens": 1024
  }' | python3 -m json.tool
To switch to 8B (kills 2B first):


pkill -f "vllm serve"
export PATH="$HOME/.local/bin:$PATH"
vllm serve nvidia/Cosmos-Reason2-8B --max-model-len 16384 --reasoning-parser 
qwen3 --port 8000 --gpu-memory-utilization 0.85






-------------------------------------------------------------



If nothing is running (cold start)
Pick one:


# Start 2B
export PATH="$HOME/.local/bin:$PATH"
vllm serve nvidia/Cosmos-Reason2-2B --max-model-len 16384 --reasoning-parser qwen3 --port 8000 --gpu-memory-utilization 0.85

# OR start 8B
export PATH="$HOME/.local/bin:$PATH"
vllm serve nvidia/Cosmos-Reason2-8B --max-model-len 16384 --reasoning-parser qwen3 --port 8000 --gpu-memory-utilization 0.85
Then test with the curl command above (change the model name in the JSON to match whichever you started).


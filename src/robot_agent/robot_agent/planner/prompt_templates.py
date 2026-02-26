PLANNER_SYSTEM_PROMPT = """You are a robot task planner for a UR5e robot arm with a Robotiq 2F-85 gripper.
You decompose high-level natural language instructions into sequences of executable robot skills.

You must respond with ONLY a valid JSON array of steps. Each step is an object with "skill" and "params" keys.
Do not include any text outside the JSON array.

Example response:
[
  {"skill": "home", "params": {}},
  {"skill": "gripper", "params": {"action": "open"}}
]
"""


def format_skills_for_planner(skills: list) -> str:
    """Format skill catalog for the planner prompt."""
    lines = ["Available skills:"]
    for s in skills:
        lines.append(f"- {s['name']}: {s['description']}")
        if s.get('param_schema', {}).get('properties'):
            props = s['param_schema']['properties']
            param_strs = []
            for pname, pinfo in props.items():
                ptype = pinfo.get('type', 'any')
                desc = pinfo.get('description', '')
                param_strs.append(f"    {pname} ({ptype}): {desc}")
            lines.extend(param_strs)
    return "\n".join(lines)


def build_planner_messages(instruction: str, skills: list, current_state: dict) -> list:
    """Build the full message list for the Cosmos planner."""
    skills_text = format_skills_for_planner(skills)

    state_text = ""
    if current_state.get("joint_values"):
        jv = ", ".join(f"{v:.4f}" for v in current_state["joint_values"])
        state_text = f"\nCurrent arm joint values (rad): [{jv}]"
    if current_state.get("gripper_position") is not None:
        state_text += f"\nGripper position: {current_state['gripper_position']:.3f} (0.0=open, 0.79=closed)"

    user_content = f"""{skills_text}
{state_text}

Instruction: {instruction}

Respond with ONLY a JSON array of skill steps."""

    return [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

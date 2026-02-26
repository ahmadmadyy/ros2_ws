import time

from rclpy.node import Node
from rclpy.action import ActionClient

from control_msgs.action import GripperCommand


GRIPPER_OPEN = 0.0
GRIPPER_CLOSED = 0.79


def _wait_for_future(future, timeout_sec: float = 30.0):
    """Block until a future is done, relying on the background executor to process it."""
    start = time.monotonic()
    while not future.done():
        if time.monotonic() - start > timeout_sec:
            return None
        time.sleep(0.05)
    return future.result()


class GripperClient:
    """Controls the Robotiq 2F-85 gripper via GripperCommand action."""

    def __init__(self, node: Node, action_topic: str = '/gripper_controller/gripper_cmd'):
        self._node = node
        self._client = ActionClient(node, GripperCommand, action_topic)

        self._node.get_logger().info(f'GripperClient: waiting for {action_topic}...')
        self._client.wait_for_server(timeout_sec=10.0)
        self._node.get_logger().info(f'GripperClient: {action_topic} available')

    def open(self) -> bool:
        """Open the gripper fully."""
        return self.set_position(GRIPPER_OPEN)

    def close(self) -> bool:
        """Close the gripper fully."""
        return self.set_position(GRIPPER_CLOSED)

    def set_position(self, position: float, max_effort: float = 0.0) -> bool:
        """Send a gripper command to the specified position."""
        goal = GripperCommand.Goal()
        goal.command.position = position
        goal.command.max_effort = max_effort

        self._node.get_logger().info(f'Gripper command: position={position:.3f}, effort={max_effort:.1f}')

        goal_handle = _wait_for_future(
            self._client.send_goal_async(goal), timeout_sec=5.0
        )
        if not goal_handle or not goal_handle.accepted:
            self._node.get_logger().error('Gripper goal rejected')
            return False

        result = _wait_for_future(goal_handle.get_result_async(), timeout_sec=10.0)
        if result is None:
            self._node.get_logger().error('Gripper result timed out')
            return False

        self._node.get_logger().info(f'Gripper reached position: {result.result.position:.3f}')
        return True

import time
from typing import List, Optional

from rclpy.node import Node
from rclpy.action import ActionClient
from rclpy.action.client import ClientGoalHandle

from moveit_msgs.action import MoveGroup, ExecuteTrajectory
from moveit_msgs.msg import (
    Constraints,
    JointConstraint,
    PositionConstraint,
    OrientationConstraint,
    BoundingVolume,
    RobotTrajectory,
)
from geometry_msgs.msg import Pose
from shape_msgs.msg import SolidPrimitive
from sensor_msgs.msg import JointState


ARM_JOINTS = [
    'shoulder_pan_joint',
    'shoulder_lift_joint',
    'elbow_joint',
    'wrist_1_joint',
    'wrist_2_joint',
    'wrist_3_joint',
]


def _wait_for_future(future, timeout_sec: float = 30.0):
    """Block until a future is done, relying on the background executor to process it."""
    start = time.monotonic()
    while not future.done():
        if time.monotonic() - start > timeout_sec:
            return None
        time.sleep(0.05)
    return future.result()


class MoveItClient:
    """Pure rclpy MoveIt2 client using MoveGroup and ExecuteTrajectory action servers."""

    def __init__(self, node: Node):
        self._node = node
        self._arm_group = 'ur_manipulator'
        self._ee_link = 'tool0'
        self._planning_frame = 'base_link'
        self._planning_time = 20.0
        self._num_attempts = 5
        self._velocity_scaling = 0.3
        self._acceleration_scaling = 0.3

        # MoveIt2 Jazzy renamed the action from /move_group to /move_action
        self._move_group_client = ActionClient(
            node, MoveGroup, '/move_action'
        )
        self._execute_client = ActionClient(
            node, ExecuteTrajectory, '/execute_trajectory'
        )

        self._node.get_logger().info('MoveItClient: waiting for /move_action action server...')
        self._move_group_client.wait_for_server(timeout_sec=10.0)
        self._node.get_logger().info('MoveItClient: /move_action available')

        self._execute_client.wait_for_server(timeout_sec=10.0)
        self._node.get_logger().info('MoveItClient: /execute_trajectory available')

    def plan_joint_goal(
        self,
        joint_values: List[float],
        velocity_scaling: float = None,
        acceleration_scaling: float = None,
    ) -> Optional[RobotTrajectory]:
        """Plan to a 6-DOF joint target. Returns trajectory or None."""
        if len(joint_values) != 6:
            self._node.get_logger().error(f'Expected 6 joint values, got {len(joint_values)}')
            return None

        vel = velocity_scaling or self._velocity_scaling
        acc = acceleration_scaling or self._acceleration_scaling

        goal = MoveGroup.Goal()
        req = goal.request
        req.group_name = self._arm_group
        req.num_planning_attempts = self._num_attempts
        req.allowed_planning_time = self._planning_time
        req.max_velocity_scaling_factor = vel
        req.max_acceleration_scaling_factor = acc

        constraints = Constraints()
        for name, value in zip(ARM_JOINTS, joint_values):
            jc = JointConstraint()
            jc.joint_name = name
            jc.position = value
            jc.tolerance_above = 0.001
            jc.tolerance_below = 0.001
            jc.weight = 1.0
            constraints.joint_constraints.append(jc)
        req.goal_constraints = [constraints]

        goal.planning_options.plan_only = True
        goal.planning_options.replan = True
        goal.planning_options.replan_attempts = 3

        self._node.get_logger().info(f'Planning joint goal: {[f"{v:.4f}" for v in joint_values]}')

        # Send goal and wait (executor is spinning in background thread)
        goal_handle = _wait_for_future(
            self._move_group_client.send_goal_async(goal), timeout_sec=15.0
        )
        if not goal_handle or not goal_handle.accepted:
            self._node.get_logger().error('MoveGroup goal rejected')
            return None

        result = _wait_for_future(goal_handle.get_result_async(), timeout_sec=30.0)
        if result is None:
            self._node.get_logger().error('MoveGroup result timed out')
            return None

        move_result = result.result
        if move_result.error_code.val != move_result.error_code.SUCCESS:
            self._node.get_logger().error(
                f'Planning failed with error code: {move_result.error_code.val}'
            )
            return None

        self._node.get_logger().info('Joint planning succeeded')
        return move_result.planned_trajectory

    def plan_pose_goal(
        self,
        pose: Pose,
        velocity_scaling: float = None,
        acceleration_scaling: float = None,
    ) -> Optional[RobotTrajectory]:
        """Plan to a Cartesian pose target. Returns trajectory or None."""
        vel = velocity_scaling or self._velocity_scaling
        acc = acceleration_scaling or self._acceleration_scaling

        goal = MoveGroup.Goal()
        req = goal.request
        req.group_name = self._arm_group
        req.num_planning_attempts = self._num_attempts
        req.allowed_planning_time = self._planning_time
        req.max_velocity_scaling_factor = vel
        req.max_acceleration_scaling_factor = acc

        constraints = Constraints()

        # Position constraint
        pc = PositionConstraint()
        pc.header.frame_id = self._planning_frame
        pc.link_name = self._ee_link
        pc.target_point_offset.x = 0.0
        pc.target_point_offset.y = 0.0
        pc.target_point_offset.z = 0.0

        bv = BoundingVolume()
        sphere = SolidPrimitive()
        sphere.type = SolidPrimitive.SPHERE
        sphere.dimensions = [0.005]
        bv.primitives.append(sphere)

        sphere_pose = Pose()
        sphere_pose.position = pose.position
        sphere_pose.orientation.w = 1.0
        bv.primitive_poses.append(sphere_pose)

        pc.constraint_region = bv
        pc.weight = 1.0
        constraints.position_constraints.append(pc)

        # Orientation constraint
        oc = OrientationConstraint()
        oc.header.frame_id = self._planning_frame
        oc.link_name = self._ee_link
        oc.orientation = pose.orientation
        oc.absolute_x_axis_tolerance = 0.01
        oc.absolute_y_axis_tolerance = 0.01
        oc.absolute_z_axis_tolerance = 0.01
        oc.weight = 1.0
        constraints.orientation_constraints.append(oc)

        req.goal_constraints = [constraints]

        goal.planning_options.plan_only = True
        goal.planning_options.replan = True
        goal.planning_options.replan_attempts = 3

        self._node.get_logger().info(
            f'Planning pose goal: pos=[{pose.position.x:.3f}, {pose.position.y:.3f}, {pose.position.z:.3f}]'
        )

        goal_handle = _wait_for_future(
            self._move_group_client.send_goal_async(goal), timeout_sec=15.0
        )
        if not goal_handle or not goal_handle.accepted:
            self._node.get_logger().error('MoveGroup pose goal rejected')
            return None

        result = _wait_for_future(goal_handle.get_result_async(), timeout_sec=30.0)
        if result is None:
            self._node.get_logger().error('MoveGroup pose result timed out')
            return None

        move_result = result.result
        if move_result.error_code.val != move_result.error_code.SUCCESS:
            self._node.get_logger().error(
                f'Pose planning failed with error code: {move_result.error_code.val}'
            )
            return None

        self._node.get_logger().info('Pose planning succeeded')
        return move_result.planned_trajectory

    def execute(self, trajectory: RobotTrajectory) -> bool:
        """Execute a planned trajectory via ExecuteTrajectory action."""
        goal = ExecuteTrajectory.Goal()
        goal.trajectory = trajectory

        self._node.get_logger().info('Executing trajectory...')

        goal_handle = _wait_for_future(
            self._execute_client.send_goal_async(goal), timeout_sec=10.0
        )
        if not goal_handle or not goal_handle.accepted:
            self._node.get_logger().error('ExecuteTrajectory goal rejected')
            return False

        result = _wait_for_future(goal_handle.get_result_async(), timeout_sec=60.0)
        if result is None:
            self._node.get_logger().error('ExecuteTrajectory result timed out')
            return False

        exec_result = result.result
        if exec_result.error_code.val != exec_result.error_code.SUCCESS:
            self._node.get_logger().error(
                f'Execution failed with error code: {exec_result.error_code.val}'
            )
            return False

        self._node.get_logger().info('Execution succeeded')
        return True

    def plan_and_execute_joints(
        self,
        joint_values: List[float],
        velocity_scaling: float = None,
        acceleration_scaling: float = None,
    ) -> bool:
        """Plan AND execute a joint goal in a single MoveGroup action call (plan_only=False).

        Using a single call avoids the two-action-call race that causes PREEMPTED/-7
        errors when multiple /execute_trajectory server instances are visible.
        """
        if len(joint_values) != 6:
            self._node.get_logger().error(f'Expected 6 joint values, got {len(joint_values)}')
            return False

        vel = velocity_scaling or self._velocity_scaling
        acc = acceleration_scaling or self._acceleration_scaling

        goal = MoveGroup.Goal()
        req = goal.request
        req.group_name = self._arm_group
        req.num_planning_attempts = self._num_attempts
        req.allowed_planning_time = self._planning_time
        req.max_velocity_scaling_factor = vel
        req.max_acceleration_scaling_factor = acc

        constraints = Constraints()
        for name, value in zip(ARM_JOINTS, joint_values):
            jc = JointConstraint()
            jc.joint_name = name
            jc.position = value
            jc.tolerance_above = 0.001
            jc.tolerance_below = 0.001
            jc.weight = 1.0
            constraints.joint_constraints.append(jc)
        req.goal_constraints = [constraints]

        # plan_only=False → move_group plans then executes in one shot
        goal.planning_options.plan_only = False
        goal.planning_options.replan = True
        goal.planning_options.replan_attempts = 3

        self._node.get_logger().info(
            f'Plan+execute joint goal: {[f"{v:.4f}" for v in joint_values]}'
        )
        return self._send_move_group_goal(goal, exec_timeout=60.0)

    def plan_and_execute_pose(
        self,
        pose: Pose,
        velocity_scaling: float = None,
        acceleration_scaling: float = None,
    ) -> bool:
        """Plan AND execute a pose goal in a single MoveGroup action call (plan_only=False)."""
        vel = velocity_scaling or self._velocity_scaling
        acc = acceleration_scaling or self._acceleration_scaling

        goal = MoveGroup.Goal()
        req = goal.request
        req.group_name = self._arm_group
        req.num_planning_attempts = self._num_attempts
        req.allowed_planning_time = self._planning_time
        req.max_velocity_scaling_factor = vel
        req.max_acceleration_scaling_factor = acc

        constraints = Constraints()

        pc = PositionConstraint()
        pc.header.frame_id = self._planning_frame
        pc.link_name = self._ee_link
        pc.target_point_offset.x = 0.0
        pc.target_point_offset.y = 0.0
        pc.target_point_offset.z = 0.0
        bv = BoundingVolume()
        sphere = SolidPrimitive()
        sphere.type = SolidPrimitive.SPHERE
        sphere.dimensions = [0.02]   # 2 cm position tolerance — enough for pick/place
        bv.primitives.append(sphere)
        sphere_pose = Pose()
        sphere_pose.position = pose.position
        sphere_pose.orientation.w = 1.0
        bv.primitive_poses.append(sphere_pose)
        pc.constraint_region = bv
        pc.weight = 1.0
        constraints.position_constraints.append(pc)

        oc = OrientationConstraint()
        oc.header.frame_id = self._planning_frame
        oc.link_name = self._ee_link
        oc.orientation = pose.orientation
        oc.absolute_x_axis_tolerance = 0.2   # ~11° — wider window for OMPL IK sampling
        oc.absolute_y_axis_tolerance = 0.2
        oc.absolute_z_axis_tolerance = 0.2
        oc.weight = 1.0
        constraints.orientation_constraints.append(oc)

        req.goal_constraints = [constraints]

        goal.planning_options.plan_only = False
        goal.planning_options.replan = True
        goal.planning_options.replan_attempts = 3

        self._node.get_logger().info(
            f'Plan+execute pose goal: pos=[{pose.position.x:.3f}, '
            f'{pose.position.y:.3f}, {pose.position.z:.3f}]'
        )
        return self._send_move_group_goal(goal, exec_timeout=60.0)

    def _send_move_group_goal(self, goal: MoveGroup.Goal, exec_timeout: float = 60.0) -> bool:
        """Send a MoveGroup goal and wait for the final result."""
        t_start = time.monotonic()

        goal_handle = _wait_for_future(
            self._move_group_client.send_goal_async(goal), timeout_sec=15.0
        )
        if not goal_handle or not goal_handle.accepted:
            self._node.get_logger().error('MoveGroup goal rejected')
            return False

        result = _wait_for_future(goal_handle.get_result_async(), timeout_sec=exec_timeout)
        elapsed = time.monotonic() - t_start

        if result is None:
            self._node.get_logger().error('MoveGroup result timed out')
            return False

        move_result = result.result
        if move_result.error_code.val != move_result.error_code.SUCCESS:
            if elapsed < 0.5:
                self._node.get_logger().error(
                    f'MoveGroup failed with code {move_result.error_code.val} after only '
                    f'{elapsed:.2f}s — this is a stale DDS action-server from a previous '
                    'bringup launch. Fix: kill the old bringup first, then restart:\n'
                    '  pkill -9 -f move_group\n'
                    '  pkill -9 -f ros2_control_node\n'
                    '  ros2 daemon stop && ros2 daemon start\n'
                    'Then relaunch bringup.launch.py and re-run this test.'
                )
            else:
                self._node.get_logger().error(
                    f'MoveGroup failed with error code: {move_result.error_code.val}'
                )
            return False

        self._node.get_logger().info('MoveGroup plan+execute succeeded')
        return True

    def allow_collision(self, object_id: str) -> bool:
        """Tell MoveIt to allow any robot link to be in contact with object_id.

        Gets the current ACM, sets all *explicit* entries involving the named
        object to True (allowed), then applies the updated ACM.  The default-entry
        alone does not work because spawn adds explicit "not allowed" entries that
        override defaults.  Call this before descending into a target object.
        """
        from moveit_msgs.srv import ApplyPlanningScene, GetPlanningScene
        from moveit_msgs.msg import PlanningScene, PlanningSceneComponents

        # ---- 1. get the current ACM ----
        get_client = self._node.create_client(GetPlanningScene, '/get_planning_scene')
        if not get_client.wait_for_service(timeout_sec=5.0):
            self._node.get_logger().error('allow_collision: /get_planning_scene not available')
            return False

        get_req = GetPlanningScene.Request()
        get_req.components.components = PlanningSceneComponents.ALLOWED_COLLISION_MATRIX
        get_result = _wait_for_future(get_client.call_async(get_req), timeout_sec=5.0)
        if get_result is None:
            self._node.get_logger().error('allow_collision: GetPlanningScene timed out')
            return False

        acm = get_result.scene.allowed_collision_matrix

        # ---- 2. enable all explicit entries for this object ----
        if object_id in acm.entry_names:
            idx = acm.entry_names.index(object_id)
            # Set every bit in the object's own row to True
            acm.entry_values[idx].enabled = [True] * len(acm.entry_names)
            # Set the object's column in every other row to True
            for i, row in enumerate(acm.entry_values):
                if i != idx and idx < len(row.enabled):
                    row.enabled[idx] = True

        # ---- 3. also set/update the default entry (catches future links) ----
        if object_id not in acm.default_entry_names:
            acm.default_entry_names.append(object_id)
            acm.default_entry_values.append(True)
        else:
            di = acm.default_entry_names.index(object_id)
            acm.default_entry_values[di] = True

        # ---- 4. apply the updated ACM ----
        scene = PlanningScene()
        scene.is_diff = True
        scene.allowed_collision_matrix = acm

        apply_client = self._node.create_client(ApplyPlanningScene, '/apply_planning_scene')
        if not apply_client.wait_for_service(timeout_sec=5.0):
            self._node.get_logger().error('allow_collision: /apply_planning_scene not available')
            return False

        req = ApplyPlanningScene.Request()
        req.scene = scene
        result = _wait_for_future(apply_client.call_async(req), timeout_sec=5.0)
        ok = result is not None and result.success
        if ok:
            self._node.get_logger().info(f"ACM: allowed all collisions with '{object_id}'")
        else:
            self._node.get_logger().error(f"ACM: failed to allow collisions with '{object_id}'")
        return ok

    def attach_object(
        self,
        object_id: str,
        link_name: str,
        touch_links: List[str] = None,
    ) -> bool:
        """Attach a world collision object to a robot link.

        Atomically removes the object from the world and adds it as an
        attached collision object.  The object remains visible in RViz and
        moves with the arm.  touch_links are robot links allowed to be in
        contact with the held object (e.g. all gripper links).
        """
        from moveit_msgs.srv import ApplyPlanningScene
        from moveit_msgs.msg import (
            PlanningScene,
            AttachedCollisionObject,
            CollisionObject,
        )

        scene = PlanningScene()
        scene.is_diff = True

        aco = AttachedCollisionObject()
        aco.link_name = link_name
        aco.object.id = object_id
        aco.object.operation = CollisionObject.ADD
        if touch_links:
            aco.touch_links = list(touch_links)
        scene.robot_state.attached_collision_objects.append(aco)
        scene.robot_state.is_diff = True
        # Note: do NOT explicitly remove from world — MoveIt moves the object
        # from world to attached automatically when processing CollisionObject.ADD
        # on an AttachedCollisionObject.  Sending a simultaneous REMOVE on the
        # world side causes the geometry lookup to fail.

        client = self._node.create_client(ApplyPlanningScene, '/apply_planning_scene')
        if not client.wait_for_service(timeout_sec=5.0):
            self._node.get_logger().error('attach_object: /apply_planning_scene not available')
            return False

        req = ApplyPlanningScene.Request()
        req.scene = scene
        result = _wait_for_future(client.call_async(req), timeout_sec=5.0)
        ok = result is not None and result.success
        if ok:
            self._node.get_logger().info(f"Attached '{object_id}' to '{link_name}'")
        else:
            self._node.get_logger().error(f"Failed to attach '{object_id}' to '{link_name}'")
        return ok

    def detach_object(self, object_id: str) -> bool:
        """Detach a previously attached collision object from the robot.

        Sends a REMOVE operation on the AttachedCollisionObject, which moves
        the object back into the world collision objects so the planner can
        avoid it again.
        """
        from moveit_msgs.srv import ApplyPlanningScene
        from moveit_msgs.msg import (
            PlanningScene,
            AttachedCollisionObject,
            CollisionObject,
        )

        scene = PlanningScene()
        scene.is_diff = True

        aco = AttachedCollisionObject()
        aco.object.id = object_id
        aco.object.operation = CollisionObject.REMOVE
        scene.robot_state.attached_collision_objects.append(aco)
        scene.robot_state.is_diff = True

        client = self._node.create_client(ApplyPlanningScene, '/apply_planning_scene')
        if not client.wait_for_service(timeout_sec=5.0):
            self._node.get_logger().error('detach_object: /apply_planning_scene not available')
            return False

        req = ApplyPlanningScene.Request()
        req.scene = scene
        result = _wait_for_future(client.call_async(req), timeout_sec=5.0)
        ok = result is not None and result.success
        if ok:
            self._node.get_logger().info(f"Detached '{object_id}' from robot")
        else:
            self._node.get_logger().error(f"Failed to detach '{object_id}' from robot")
        return ok

    def ik(
        self,
        pose: Pose,
        seed_joints: List[float] = None,
        timeout_sec: float = 5.0,
    ) -> Optional[List[float]]:
        """Call /compute_ik to get arm joint values for a Cartesian pose.

        Returns a list of 6 joint values (ARM_JOINTS order) or None on failure.
        Passing seed_joints biases the solver toward that configuration.
        """
        from moveit_msgs.srv import GetPositionIK
        from moveit_msgs.msg import PositionIKRequest, RobotState
        from sensor_msgs.msg import JointState as SJointState
        from builtin_interfaces.msg import Duration

        client = self._node.create_client(GetPositionIK, '/compute_ik')
        if not client.wait_for_service(timeout_sec=5.0):
            self._node.get_logger().error('IK: /compute_ik service not available')
            return None

        ik_req = PositionIKRequest()
        ik_req.group_name = self._arm_group
        ik_req.ik_link_name = self._ee_link
        ik_req.pose_stamped.header.frame_id = self._planning_frame
        ik_req.pose_stamped.pose = pose
        ik_req.timeout = Duration(sec=int(timeout_sec))

        if seed_joints is not None:
            rs = RobotState()
            js = SJointState()
            js.name = list(ARM_JOINTS)
            js.position = list(seed_joints)
            rs.joint_state = js
            ik_req.robot_state = rs

        req = GetPositionIK.Request()
        req.ik_request = ik_req

        result = _wait_for_future(client.call_async(req), timeout_sec=timeout_sec + 2.0)
        if result is None:
            self._node.get_logger().error('IK: service call timed out')
            return None

        if result.error_code.val != 1:  # 1 = SUCCESS
            self._node.get_logger().error(
                f'IK failed (code {result.error_code.val}) for '
                f'pos=[{pose.position.x:.3f}, {pose.position.y:.3f}, {pose.position.z:.3f}] '
                f'ori=[{pose.orientation.x:.3f}, {pose.orientation.y:.3f}, '
                f'{pose.orientation.z:.3f}, {pose.orientation.w:.3f}]'
            )
            return None

        js = result.solution.joint_state
        name_to_pos = dict(zip(js.name, js.position))
        try:
            vals = [name_to_pos[j] for j in ARM_JOINTS]
            self._node.get_logger().info(
                f'IK solution: [{", ".join(f"{v:.3f}" for v in vals)}]'
            )
            return vals
        except KeyError as exc:
            self._node.get_logger().error(f'IK: result missing joint {exc}')
            return None

    def get_current_joint_values(self, latest_joint_state: Optional[JointState]) -> Optional[List[float]]:
        """Extract arm joint values from a JointState message."""
        if latest_joint_state is None:
            return None
        name_to_pos = dict(zip(latest_joint_state.name, latest_joint_state.position))
        try:
            return [name_to_pos[j] for j in ARM_JOINTS]
        except KeyError:
            return None

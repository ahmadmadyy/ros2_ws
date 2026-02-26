#!/usr/bin/env python3
"""
Adds static planning-scene objects (ground plane + screwdriver) to MoveIt2 at startup.

Launched automatically by bringup.launch.py via OnProcessStart(move_group_node).
The node waits for the /apply_planning_scene service, adds the objects, then exits.
Objects persist in move_group memory for the lifetime of the move_group process.
"""

import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Pose
from shape_msgs.msg import SolidPrimitive
from std_msgs.msg import ColorRGBA

from moveit_msgs.msg import CollisionObject, ObjectColor, PlanningScene
from moveit_msgs.srv import ApplyPlanningScene


# ---------------------------------------------------------------------------
# Object definitions — edit here to change the workspace layout
# ---------------------------------------------------------------------------
SCREWDRIVER_X = 0.45   # m — in front of the robot base
SCREWDRIVER_Y = 0.00   # m — centred on the robot's forward axis
SCREWDRIVER_Z = 0.09   # m — centre of the cylinder (half of height=0.18)
SCREWDRIVER_HEIGHT = 0.18   # m
SCREWDRIVER_RADIUS = 0.012  # m

GROUND_Z = -0.02       # m — centre of the ground-plane box
GROUND_H = 0.04        # m — thickness of the ground box


def _make_pose(x: float, y: float, z: float) -> Pose:
    p = Pose()
    p.position.x = x
    p.position.y = y
    p.position.z = z
    p.orientation.w = 1.0
    return p


class SceneObjectSpawner(Node):
    def __init__(self):
        super().__init__("scene_object_spawner")

    def spawn(self) -> bool:
        self.get_logger().info("Waiting for /apply_planning_scene service…")
        client = self.create_client(ApplyPlanningScene, "/apply_planning_scene")
        if not client.wait_for_service(timeout_sec=30.0):
            self.get_logger().error(
                "Timed out waiting for /apply_planning_scene — is move_group running?"
            )
            return False

        scene = PlanningScene()
        scene.is_diff = True

        # ---- ground plane ----
        ground = CollisionObject()
        ground.id = "ground_plane"
        ground.header.frame_id = "world"
        ground.operation = CollisionObject.ADD
        gp = SolidPrimitive()
        gp.type = SolidPrimitive.BOX
        gp.dimensions = [2.0, 2.0, GROUND_H]
        ground.primitives.append(gp)
        ground.primitive_poses.append(_make_pose(0.0, 0.0, GROUND_Z))
        scene.world.collision_objects.append(ground)

        # ---- screwdriver (standing cylinder) ----
        sd = CollisionObject()
        sd.id = "screwdriver"
        sd.header.frame_id = "world"
        sd.operation = CollisionObject.ADD
        cyl = SolidPrimitive()
        cyl.type = SolidPrimitive.CYLINDER
        cyl.dimensions = [SCREWDRIVER_HEIGHT, SCREWDRIVER_RADIUS]
        sd.primitives.append(cyl)
        sd.primitive_poses.append(_make_pose(SCREWDRIVER_X, SCREWDRIVER_Y, SCREWDRIVER_Z))
        scene.world.collision_objects.append(sd)

        # ---- colour: screwdriver → red ----
        col = ObjectColor()
        col.id = "screwdriver"
        col.color = ColorRGBA(r=0.8, g=0.2, b=0.2, a=1.0)
        scene.object_colors.append(col)

        req = ApplyPlanningScene.Request()
        req.scene = scene

        self.get_logger().info(
            f"Adding ground_plane and screwdriver "
            f"[x={SCREWDRIVER_X}, y={SCREWDRIVER_Y}, z={SCREWDRIVER_Z}] to planning scene…"
        )
        future = client.call_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)

        if future.result() and future.result().success:
            self.get_logger().info("Planning scene objects added successfully.")
            return True
        else:
            self.get_logger().error("apply_planning_scene call failed.")
            return False


def main():
    rclpy.init()
    node = SceneObjectSpawner()
    node.spawn()
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()

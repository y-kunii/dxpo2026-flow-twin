// Copyright
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Reference:
// https://github.com/ros-planning/moveit2_tutorials/blob
// /a547cf49ff7d1fe16a93dfe020c6027bcb035b51/doc/move_group_interface
// /src/move_group_interface_tutorial.cpp
// https://docs.ros.org/en/humble/Tutorials/Intermediate/Tf2/Writing-A-Tf2-Listener-Cpp.html

#include <chrono>
#include <cmath>
#include <memory>

#include "angles/angles.h"
#include "geometry_msgs/msg/pose.hpp"
#include "geometry_msgs/msg/quaternion.hpp"
#include "geometry_msgs/msg/transform_stamped.hpp"
#include "geometry_msgs/msg/twist.hpp"
#include "moveit/move_group_interface/move_group_interface.h"
#include "rclcpp/rclcpp.hpp"
#include "tf2_geometry_msgs/tf2_geometry_msgs.hpp"
#include "tf2/convert.h"
#include "tf2/exceptions.h"
#include "tf2_ros/transform_listener.h"
#include "tf2_ros/buffer.h"
#include "std_msgs/msg/string.hpp"
using namespace std::chrono_literals;
using MoveGroupInterface = moveit::planning_interface::MoveGroupInterface;

class PickAndPlaceDemo : public rclcpp::Node
{
public:
  PickAndPlaceDemo(
    rclcpp::Node::SharedPtr move_group_arm_node,
    rclcpp::Node::SharedPtr move_group_gripper_node)
  : Node("pick_and_place_demo_node")
  {
    using namespace std::placeholders;
    move_group_arm_ = std::make_shared<MoveGroupInterface>(move_group_arm_node, "arm");
    move_group_arm_->setMaxVelocityScalingFactor(1.0);
    move_group_arm_->setMaxAccelerationScalingFactor(1.0);

    move_group_gripper_ = std::make_shared<MoveGroupInterface>(move_group_gripper_node, "gripper");
    move_group_gripper_->setMaxVelocityScalingFactor(1.0);
    move_group_gripper_->setMaxAccelerationScalingFactor(1.0);

    // publisher
    operating_status_publisher_ =
      this->create_publisher<std_msgs::msg::String>("operating_status_topic", 10);
    gripper_status_publisher_ =
      this->create_publisher<std_msgs::msg::String>("gripper_status_topic", 10);

    // SRDFに定義されている"home"の姿勢にする
    move_group_arm_->setNamedTarget("home");
    move_group_arm_->move();

    // 可動範囲を制限する
    moveit_msgs::msg::Constraints constraints;
    constraints.name = "arm_constraints";

    moveit_msgs::msg::JointConstraint joint_constraint;
    joint_constraint.joint_name = "crane_plus_joint1";
    joint_constraint.position = 0.0;
    joint_constraint.tolerance_above = angles::from_degrees(100);
    joint_constraint.tolerance_below = angles::from_degrees(100);
    joint_constraint.weight = 1.0;
    constraints.joint_constraints.push_back(joint_constraint);

    joint_constraint.joint_name = "crane_plus_joint3";
    joint_constraint.position = 0.0;
    joint_constraint.tolerance_above = angles::from_degrees(0);
    joint_constraint.tolerance_below = angles::from_degrees(180);
    joint_constraint.weight = 1.0;
    constraints.joint_constraints.push_back(joint_constraint);

    move_group_arm_->setPathConstraints(constraints);

    // 待機姿勢
    control_arm(0.0, 0.0, 0.17, 0, 0, 0);
    operating_status_ = "idle";
    gripper_status_ = "close";

    // 定期通知(主にidle時に稼働)
    //timer_ = this->create_wall_timer(
    //  500ms, std::bind(&PickAndPlaceDemo::on_timer, this));

    // モーション要求受信
    subscription_ = this->create_subscription<std_msgs::msg::String>(
      "pick_and_place_topic", 1, std::bind(&PickAndPlaceDemo::pick_and_place_callback, this, _1));
  }

private:
  struct pose {
    double x;
    double y;
    double z;
    double roll;
    double pitch;
    double yaw;
  };

  const double GRIPPER_DEFAULT = 0.0;
  const double GRIPPER_OPEN = angles::from_degrees(-30.0);
  const double GRIPPER_CLOSE = angles::from_degrees(10.0);

  void publish_operating_status()
  {
    std::unique_ptr<std_msgs::msg::String> operating_msg = std::make_unique<std_msgs::msg::String>();
    operating_msg->data = operating_status_;
    operating_status_publisher_->publish(std::move(operating_msg));
  }

  void publish_gripper_status()
  {
    std::unique_ptr<std_msgs::msg::String> gripper_msg = std::make_unique<std_msgs::msg::String>();
    gripper_msg->data = gripper_status_;
    gripper_status_publisher_->publish(std::move(gripper_msg));
  }

  void on_timer()
  {
    publish_operating_status();
    publish_gripper_status();
  }

  void pick_and_place_callback(const std_msgs::msg::String::SharedPtr msg)
  {
    operating_status_ = "operating";
    publish_operating_status();

    const tf2::Vector3 place1 = tf2::Vector3(0.2, -0.15, 0.0);
    const tf2::Vector3 place2 = tf2::Vector3(0.2,  0.0,  0.0);
    const tf2::Vector3 place3 = tf2::Vector3(0.2,  0.15, 0.0);
    
    if (msg->data == "motion1") {
      picking(place1);
    } else if (msg->data == "motion2") {
      picking(place2);
    } else if (msg->data == "motion3") {
      picking(place3);
    } else if (msg->data == "motion4") {
      put_back(place1);
    } else if (msg->data == "motion5") {
      put_back(place2);
    } else if (msg->data == "motion6") {
      put_back(place3);
    } else if (msg->data == "pose1") {
      to_pose(place1);
    } else if (msg->data == "pose2") {
      to_pose(place2);
    } else if (msg->data == "pose3") {
      to_pose(place3);
    } else if (msg->data == "pose_handover") {
      control_gripper(GRIPPER_OPEN);
      control_arm(0.0, 0.15, 0.05, 0, 90, -270);
    }

    operating_status_ = "idle";
    publish_operating_status();
    return;
  }

  void picking(tf2::Vector3 target_position)
  {
    // 何かを掴んでいた時のためにハンドを開く
    control_gripper(GRIPPER_OPEN);

    pose pickup_pose;
    calc_ground_pose(target_position, pickup_pose);

    // 把持対象物に正対する
    control_arm(0.0, 0.0, 0.17, 0, 90, pickup_pose.yaw);

    if (!control_arm(pickup_pose)) {
      // アーム動作に失敗した時はpick_and_placeを中断して待機姿勢に戻る
      control_arm(0.0, 0.0, 0.17, 0, 0, 0);
      return;
    }

    // ハンドを閉じる
    control_gripper(GRIPPER_CLOSE);

    // 移動する
    control_arm(0.0, 0.0, 0.17, 0, 90, 0);

    // 下ろす
    //control_arm(0.0, -0.15, 0.05, 0, 90, -90);
    control_arm(0.0, 0.15, 0.05, 0, 90, -270);

    // ハンドを開く
    control_gripper(GRIPPER_OPEN);

    // [キビキビ化A-1] 「少し持ち上げ」は省略し、離したら直接待機姿勢へ
    //control_arm(0.0, 0.1, 0.1, 0, 90, -270);

    // 待機姿勢に戻る
    control_arm(0.0, 0.0, 0.17, 0, 0, 0);

    // [キビキビ化A-1] ループでは次動作の冒頭でハンドを開くので、末尾の閉じは省略
    //control_gripper(GRIPPER_DEFAULT);
  }


  void put_back(tf2::Vector3 target_position)
  {
    // 何かを掴んでいた時のためにハンドを開く
    control_gripper(GRIPPER_OPEN);

    // 把持対象物に正対する
    control_arm(0.0, 0.0, 0.17, 0, 90, -270);

    // 掴みに行く
    control_arm(0.0, 0.15, 0.04, 0, 90, -270);

    // ハンドを閉じる
    control_gripper(GRIPPER_CLOSE);

    // 移動する
    control_arm(0.0, 0.0, 0.17, 0, 90, 0);

    pose place_pose;
    calc_ground_pose(target_position, place_pose);

    // 下ろす
    if (!control_arm(place_pose)) {
      // アーム動作に失敗した時はpick_and_placeを中断して待機姿勢に戻る
      control_arm(0.0, 0.0, 0.17, 0, 0, 0);
      return;
    }

    // ハンドを開く
    control_gripper(GRIPPER_OPEN);

    // [キビキビ化A-1] 「少し持ち上げ」は省略し、離したら直接待機姿勢へ
    //control_arm(place_pose.x, place_pose.y, 0.10, place_pose.roll, place_pose.pitch, place_pose.yaw);

    // 待機姿勢に戻る
    control_arm(0.0, 0.0, 0.17, 0, 0, 0);

    // [キビキビ化A-1] ループでは次動作の冒頭でハンドを開くので、末尾の閉じは省略
    //control_gripper(GRIPPER_DEFAULT);
  }


  void to_pose(tf2::Vector3 target_position)
  {
    // 何かを掴んでいた時のためにハンドを開く
    control_gripper(GRIPPER_OPEN);

    pose ground_pose;
    calc_ground_pose(target_position, ground_pose);

    control_arm(ground_pose);
  }

  void calc_ground_pose(tf2::Vector3 target_position, pose &ground_pose)
  {
    // ロボット座標系（2D）の原点から見た把持対象物への角度を計算
    double x = target_position.x();
    double y = target_position.y();
    double theta_rad = std::atan2(y, x);
    const double GRIPPER_OFFSET = 0.13;

    ground_pose.x = x - GRIPPER_OFFSET * std::cos(theta_rad);
    ground_pose.y = y - GRIPPER_OFFSET * std::sin(theta_rad);
    ground_pose.z = 0.04;
    ground_pose.roll = 0;
    ground_pose.pitch = 90;
    ground_pose.yaw = theta_rad * 180.0 / 3.1415926535;
  }

  // グリッパ制御
  void control_gripper(const double angle)
  {
    auto joint_values = move_group_gripper_->getCurrentJointValues();
    joint_values[0] = angle;
    move_group_gripper_->setJointValueTarget(joint_values);
    move_group_gripper_->move();

    if (angle < 0) {
      gripper_status_ = "open";
    } else {
      gripper_status_ = "close";
    }
    publish_gripper_status();
  }

  bool control_arm(const pose req)
  {
    return control_arm(req.x, req.y, req.z, req.roll, req.pitch, req.yaw);
  }

  // アーム制御
  bool control_arm(
    const double x, const double y, const double z,
    const double roll, const double pitch, const double yaw)
  {
    geometry_msgs::msg::Pose target_pose;
    tf2::Quaternion q;
    target_pose.position.x = x;
    target_pose.position.y = y;
    target_pose.position.z = z;
    q.setRPY(angles::from_degrees(roll), angles::from_degrees(pitch), angles::from_degrees(yaw));
    target_pose.orientation = tf2::toMsg(q);
    move_group_arm_->setPoseTarget(target_pose);
    // アーム動作の成否を取得
    moveit::core::MoveItErrorCode result = move_group_arm_->move();
    return result.val == moveit::core::MoveItErrorCode::SUCCESS;
  }

  std::shared_ptr<MoveGroupInterface> move_group_arm_;
  std::shared_ptr<MoveGroupInterface> move_group_gripper_;

  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr subscription_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr operating_status_publisher_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr gripper_status_publisher_;
  rclcpp::TimerBase::SharedPtr timer_{nullptr};

  std::string operating_status_;
  std::string gripper_status_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  rclcpp::NodeOptions node_options;
  node_options.automatically_declare_parameters_from_overrides(true);
  auto move_group_arm_node = rclcpp::Node::make_shared("move_group_arm_node", node_options);
  auto move_group_gripper_node = rclcpp::Node::make_shared("move_group_gripper_node", node_options);

  rclcpp::executors::MultiThreadedExecutor exec;
  auto pick_and_place_demo_node = std::make_shared<PickAndPlaceDemo>(
    move_group_arm_node,
    move_group_gripper_node);
  exec.add_node(pick_and_place_demo_node);
  exec.add_node(move_group_arm_node);
  exec.add_node(move_group_gripper_node);
  exec.spin();
  rclcpp::shutdown();
  return 0;
}

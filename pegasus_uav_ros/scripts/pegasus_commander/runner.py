import numpy as np
import threading
from io import BytesIO

import messages.pegasus_messages_pb2 as messages_pb2
import pegasus_verify_data as verify_data
import rospy
from tf.transformations import euler_from_quaternion


def create_return_to_home_message():
    request = messages_pb2.Request()
    request.id = 1
    request.command = messages_pb2.Command.SET_RETURN_TO_HOME
    return request


def get_yaw_from_quaternion(orientation):
    yaw = euler_from_quaternion(
        [orientation.x,
         orientation.y,
         orientation.z,
         orientation.w])[2]
    if yaw < 0:
        yaw = 2 * np.pi - (-yaw % 2 * np.pi)
    else:
        yaw = yaw % 2 * np.pi
    return yaw


def is_at_position(expected_pose, actual_pose, offset_distance, offset_yaw):
    """ Actually its PoseStamped"""
    desired = np.array((expected_pose.pose.position.x,
                        expected_pose.pose.position.y,
                        expected_pose.pose.position.z))
    actual = np.array((actual_pose.pose.position.x,
                       actual_pose.pose.position.y,
                       actual_pose.pose.position.z))
    real_offset_distance = np.linalg.norm(desired - actual)
    desired_yaw = get_yaw_from_quaternion(expected_pose.pose.orientation)
    actual_yaw = get_yaw_from_quaternion(actual_pose.pose.orientation)
    real_yaw_difference = abs(desired_yaw - actual_yaw)
    rospy.loginfo_throttle(1, "desired %s actual %s difference %s" % (desired_yaw, actual_yaw, real_yaw_difference))
    return real_offset_distance < offset_distance and real_yaw_difference < offset_yaw


class Runner(threading.Thread):
    def __init__(self, commander):
        threading.Thread.__init__(self)
        self.commander = commander
        self.data = None
        self.socket = None
        self.clientAddress = None
        self.rate = rospy.Rate(20)
        self.request_offboard_time = None
        self.request_arm_time = None
        self.request_rtl_time = None

    def set_data(self, data):
        self.data = data
        return self

    def set_socket(self, socket):
        self.socket = socket
        return self

    def set_client_address(self, client_address):
        self.clientAddress = client_address
        return self

    def request_offboard(self, request):
        self.commander.running = True
        now = rospy.get_rostime()
        while self.commander.mavros_gw.get_mavros_state().mode != 'OFFBOARD':
            if self.request_offboard_time is not None and now - self.request_offboard_time < rospy.Duration(5):
                self.rate.sleep()
                continue
            rospy.loginfo('%s %s' % (self.commander.namespace, self.commander.mavros_gw.get_mavros_state().mode))
            for i in range(100):
                self.commander.mavros_gw.set_mavros_local_pose(self.commander.current_pose)
                self.rate.sleep()
            try:
                res = self.commander.mavros_gw.mavros_service['set_mode'](
                    base_mode=0, custom_mode='OFFBOARD')
                if not res.mode_sent:
                    rospy.logerr("failed to send mode command")
                    self.request_offboard_time = now
            except rospy.ServiceException as e:
                rospy.logerr(e)
                self.request_offboard_time = now
            if not self.commander.running:
                break
            self.rate.sleep()

    def request_arm(self, request):
        self.commander.running = True
        now = rospy.get_rostime()
        while not self.commander.mavros_gw.get_mavros_state().armed:
            if self.request_arm_time is not None and now - self.request_arm_time < rospy.Duration(5):
                self.rate.sleep()
                continue
            try:
                res = self.commander.mavros_gw.mavros_service['arming'](True)
                if not res.success:
                    rospy.logerr("failed to send arm command")
                    self.request_arm_time = now
            except rospy.ServiceException as e:
                self.request_arm_time = now
                rospy.logerr(e)
            if not self.commander.running:
                break
            self.rate.sleep()

    def request_return_to_home(self, request):
        now = rospy.get_rostime()
        while self.commander.mavros_gw.get_mavros_state().mode != 'AUTO.RTL':
            if self.request_rtl_time is not None and now - self.request_rtl_time < rospy.Duration(5):
                self.rate.sleep()
                continue
            try:
                res = self.commander.mavros_gw.mavros_service['set_mode'](
                    base_mode=0, custom_mode='AUTO.RTL')
                if not res.mode_sent:
                    rospy.logerr("failed to send mode command")
                    self.request_rtl_time = now
            except rospy.ServiceException as e:
                rospy.logerr(e)
                self.request_rtl_time = now
            self.rate.sleep()

    def request_goto(self, request):
        self.commander.running = True
        self.commander.current_pose.deserialize(request.req_data)
        while not rospy.is_shutdown():
            actual_pose = self.commander.mavros_gw.get_mavros_local_pose()
            expected_pose = self.commander.current_pose
            # half a meter, 0.5 degrees
            degrees = 0.5 * np.pi/180
            if is_at_position(expected_pose, actual_pose, 0.5, degrees):
                return
            self.commander.mavros_gw.set_mavros_local_pose(expected_pose)
            if not self.commander.running:
                break
            self.rate.sleep()

    def request_heartbeat(self, request):
        self.commander.heartbeat_time = rospy.get_rostime()
        self.commander.return_to_home = False
        reply = messages_pb2.Reply()
        reply.timestamp = int(self.commander.heartbeat_time.to_sec())
        m_state = self.commander.mavros_gw.get_mavros_state()
        m_local_pose = self.commander.mavros_gw.get_mavros_local_pose()
        m_gps = self.commander.mavros_gw.get_mavros_global_gps()
        # m_bl_trans = self.commander.mavros_gw.get_mavros_base_link_transform()
        m_bl_trans = None

        b_state = BytesIO()
        b_local_pose = BytesIO()
        b_gps = BytesIO()
        b_bl_trans = BytesIO()

        if m_state is not None:
            m_state.serialize(b_state)
            reply.heartbeat_data.mavros_state = b_state.getvalue()

        if m_local_pose is not None:
            m_local_pose.serialize(b_local_pose)
            reply.heartbeat_data.local_pose = b_local_pose.getvalue()

        if m_gps is not None:
            m_gps.serialize(b_gps)
            reply.heartbeat_data.gps_nav_sat = b_gps.getvalue()

        if m_bl_trans is not None:
            m_bl_trans.serialize(b_bl_trans)
            reply.heartbeat_data.base_link_transform = b_bl_trans.getvalue()

        l_cmd_id, l_cmd_completed = self.commander.get_last_command()
        reply.heartbeat_data.last_command_id = l_cmd_id
        reply.heartbeat_data.last_command_completed = l_cmd_completed

        data = reply.SerializeToString()
        rospy.loginfo('%s: Reply size %s', self.commander.namespace, len(data))
        msg = verify_data.pack_msg(data)
        self.socket.sendto(msg, self.clientAddress)

    def run(self):
        request_processors = {
            messages_pb2.Command.SET_OFFBOARD: self.request_offboard,
            messages_pb2.Command.SET_ARM: self.request_arm,
            messages_pb2.Command.SET_RETURN_TO_HOME: self.request_return_to_home,
            messages_pb2.Command.GOTO: self.request_goto,
        }
        request = messages_pb2.Request()
        try:
            request.ParseFromString(self.data)
        except Exception as e:
            rospy.logerr(str(e) + ' data: ' + self.data)
            return
        print (request)
        if request.command in request_processors.keys():
            # Run one request at a time
            if self.commander.cmd_server_lock.acquire(False):
                self.commander.set_last_command(request.id, False)
                request_processors[request.command](request)
                self.commander.cmd_server_lock.release()
                self.commander.set_last_command(request.id, True)
        elif request.command == messages_pb2.Command.HEARTBEAT:
            # Heartbeat can be asynchronous
            self.request_heartbeat(request)

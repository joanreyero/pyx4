#!/usr/bin/env python
from __future__ import print_function

""" ROS node to perform most of the testing logic.
- Manage subscriptions to relevant topics
- Parse all the data needed for testing
- Do the testing
- All the results are published to the /pyx4_test topic
"""

PKG = 'pyx4'
NAME = 'pyx4_test'

import sys, time, os, csv
import numpy as np
import rospy
from pyx4.msg import pyx4_state as Pyx4_msg
from pyx4.msg import pyx4_test as Pyx4_test_msg
from geometry_msgs.msg import PoseStamped, TwistStamped
from mavros_msgs.msg import PositionTarget
from pyx4_base.definitions_pyx4 import TEST_COMP, MISSION_SPECS
from pyx4_base.setpoint_bitmasks import *

class Pyx4Test():
    """ Class to handle the main logic, subscribers and publishers
    for Pyx4 unit testing.
    """
    def __init__(self, mission_file, comp_file):
        # Position for each waypoint
        self.wpts = Pyx4Test._parse_comp_file(comp_file)
        # Expected timeout, type and velocity for each waypoint
        (self.timeouts,
         self.types,
         self.velocities) = Pyx4Test._parse_mission_file(mission_file)

        self.total_wpts = len(self.wpts) + 3
        # Type masks for each waypoint
        self.type_masks = {}
        # Start time of the current waypoint
        self.wpt_start_time = 0
        # Index of the current waypoint
        self.current_wpt = 0
        # List of velocities for the current waypoint
        self.cb_vels = np.empty((0,2), float)
        # Current local position of the drone
        self.current_pos = []

        # Publisher for pyx4_test
        self.pyx4_test_pub = rospy.Publisher(NAME + '/pyx4_test',
                                             Pyx4_test_msg, queue_size=10)
        # Test types
        self.test_types = {'type': 'target_type',
                           'wpt_position': 'wpt_position',
                           'velocity': 'average_velocity',
                           'timeout': 'timeout'}

    @staticmethod
    def _parse_comp_file(comp_file):
        """ Read the test comparison file and return a dictionary of arrays,
        one for each waypoint.
        :param comp_file: a CSV file (label, x, y, z, yaw)
        :return {index: Array(x, y, z, yaw)}
        """
        with open(comp_file, 'r') as f:
            reader = csv.DictReader(f)
            return {i+3: np.array(map(float, [dic['x'],
                                             dic['y'],
                                             dic['z'],
                                             dic['yaw']]))
                    for i, dic in enumerate(reader)}

    @staticmethod
    def _parse_mission_file(comp_file):
        """ Read the test comparison file and return a dictionary of arrays,
        one for each waypoint.
        :param comp_file: a CSV file (label, x, y, z, yaw)
        :return {index: Array(x, y, z, yaw)}
        """
        timeouts, targets, velocities = {}, {}, {}
        last_pos = np.array([0, 0])
        with open(comp_file, 'r') as f:
            reader = csv.DictReader(f)
            for i, dic in enumerate(reader):
                # Arming and takeoff states not in mission file,
                # hence we need to shift everything by 3.
                iwpt = i + 3
                # Getting the timeout
                timeouts[iwpt] = int(dic['timeout'])
                
                xy = dic['xy_type']
                z = dic['z_type']
                yaw = dic['yaw_type']

                # Get the velocity at each waypoint if
                # xy_type is velocity.
                x = float(dic['x_setpoint'])
                y = float(dic['y_setpoint'])
                # If type velocity and not still.
                if xy == 'vel' and (x > 0 or y > 0):
                    velocities[iwpt] = np.array([x, y])
                else: velocities[iwpt] = None
                                
                # Getting the bitmask
                if xy  == 'pos' and z == 'pos' and yaw == 'pos':
                    targets[iwpt] = MASK_XY_POS__Z_POS_YAW_POS
                elif xy == 'pos' and z == 'pos' and yaw == 'vel':
                    targets[iwpt] = MASK_XY_POS__Z_POS_YAW_RATE
                elif xy == 'vel' and z == 'pos' and yaw == 'pos':
                    targets[iwpt] = MASK_XY_VEL__Z_POS__YAW_POS
                elif xy == 'vel' and z == 'pos' and yaw == 'vel':
                    targets[iwpt] = MASK_XY_VEL__Z_POS_YAW_RATE
                elif xy == 'vel' and z == 'vel' and yaw == 'pos':
                    targets[iwpt] = MASK_XY_VEL__Z_VEL_YAW_POS
                elif xy == 'vel' and z == 'vel' and yaw == 'vel':
                    targets[iwpt] = MASK_XY_VEL__Z_VEL_YAW_RATE
                elif xy == 'pos' and z == 'vel' and yaw == 'pos':
                    targets[iwpt] = MASK_XY_POS__Z_VEL_YAW_POS
                    
        return timeouts, targets, velocities

    def perform_test_pred(self):
        """ Function to see whether the tests should be performed.
        :return Bool
        """
        return self.current_wpt < self.total_wpts and self.current_wpt >= 3
    
    def type_test(self):
        """ Test to check whether the setpoint types.
        Calls send_message to publish the result in the /pyx4_test topic.
        """
        if self.perform_test_pred():
            # Get the type mask that has been published the most
            # for this waypoint
            type_mask = max(self.type_masks[self.current_wpt],
                            key=lambda x: self.type_masks[self.current_wpt][x])
            passed = (self.types[self.current_wpt] == type_mask)
            
            self.send_message(self.test_types['type'], passed,
                              self.types[self.current_wpt],
                              type_mask)

    def wpt_position_test(self):
        """ Test to check whether the position of the drone when it
        reaches each waypoint is correct.
        Calls send_message to publish the result in the /pyx4_test topic.
        """
        if self.perform_test_pred():
            # Compare all elements of both arrays
            passed = np.allclose(self.wpts[self.current_wpt],
                                 self.current_pos,
                                 rtol=2, atol=1)
        

            # Round to 2 decimal places for reporting.
            expected = list(map(lambda x: round(x, 2),
                                self.wpts[self.current_wpt]))
            given = list(map(lambda x: round(x, 2), self.current_pos))
        
            self.send_message(self.test_types['wpt_position'], passed,
                              expected, given)

    def velocity_test(self, cb_vels):
        """ Test to check whether the x and y velocity for setpoints
        of type velocity is more or less constant and as specified.
        Calls send_message to publish the result in the /pyx4_test topic.
        :param cb_vels: a list of the velocities the drone has flown at.
        """
        if (self.perform_test_pred() and
            self.velocities[self.current_wpt] is not None):
            cb_vels = cb_vels[35:-35]
            passed = np.allclose(cb_vels, self.velocities[self.current_wpt],
                                 rtol=1.2, atol=0.1)
            self.send_message(self.test_types['velocity'], passed,
                              True, passed)

    def timeout_test(self):
        """ Test to check whether all the timeouts are being followed.
        Calls send_message to publish the result in the /pyx4_test topic.
        """
        if self.current_wpt < self.total_wpts and self.current_wpt >= 3:
            expected_to = self.timeouts[self.current_wpt]
            # If we have spent more time than timeout, with 10% margin
            if time.time() - self.wpt_start_time > expected_to * 1.1:
                passed = False
                given = 'more'
            else: passed, given = True, expected_to
            # Send message
            self.send_message(self.test_types['timeout'], passed,
                          expected_to, given)

    def send_message(self, test_type, passed, expected, given):
        """ Construnct a message personalised to each test type
        and to whether the test has passed. Then publish the message
        and log it to the console.
        :param test_type (string): wpt_position, type...
        :param passed (Bool): whether the test has passed
        :param expected: an expected test result
        :param given: the actual test result
        """
        # Variables to generate the message
        passed_msg = ['FAILED', 'PASSED']
        expected_msg = {self.test_types['wpt_position']: 'to finish at',
                        self.test_types['type']: 'type mask',
                        self.test_types['velocity']: '',
                        self.test_types['timeout']: 'to finish in'}
        
        description = """Waypoint {}: {} TEST {}
        Waypoint {} {} the {} test.
        Expected {} {} and got {}
        """.format(self.current_wpt,  # Current waypoint
                   test_type.upper(),  # Test type
                   passed_msg[passed],  # FAILED / PASSED
                   self.current_wpt,  # Current wpt
                   passed_msg[passed],  # FAILED / PASSED
                   test_type,  # Test type
                   expected_msg[test_type],  # type mask, to finish at...
                   expected, given)  # Expected and given values

        # Create the Pyx4 test message and publish
        msg = Pyx4_test_msg()
        msg.test_type = test_type
        msg.waypoint = str(self.current_wpt)
        msg.passed = passed
        msg.description = description
        self.pyx4_test_pub.publish(msg)
        # Show normally if passed,
        if passed: rospy.loginfo(description)
        # Or as an error otherwise
        else: rospy.logerr(description)

    def pyx4_callback(self, data):
        """ Callback triggered every time the drone reaches a waypoint.
        Calls all the test functions and updates the required attributes.
        :param data: pyx4_state message from /pyx4_node/pyx4_state
        """
        self.type_test()
        self.wpt_position_test()
        self.velocity_test(self.cb_vels)
        self.timeout_test()
        
        self.wpt_start_time = time.time()
        self.cb_vels = np.empty((0,2), float)
        self.current_wpt += 1

    def local_position_callback(self, data):
        """ ROS subscription callback that updates the attribute
        current_pos.
        :param data: PoseStamped from /mavros/local_position/pose
        """
        # Update the current position 
        pos = data.pose.position
        self.current_pos = np.array([pos.x, pos.y, pos.z,
                                     data.pose.orientation.z])
            
    def position_target_callback(self, data):
        """ ROS subscription callback that gets a target type and
        adds it to a dictionary containing a counter of each type.
        for each waypoint.
        :param data: PositionTarget from /mavros/setpoint_raw/local
        """
        tm = data.type_mask
        # If we have not seen the current waypoint yet,
        # add it to the data
        if self.current_wpt not in self.type_masks.keys():
            self.type_masks[self.current_wpt] = {}
        
        if tm in self.type_masks[self.current_wpt].keys():
            self.type_masks[self.current_wpt][tm] += 1
        else:
            self.type_masks[self.current_wpt][tm] = 1

    def local_position_vel_callback(self, data):
        """ ROS subscription callback that adds the current velocity to
        an array of velocities.
        :param data: TwistStamped from /mavros/local_position/velocity_local
        """
        vel = data.twist.linear
        self.cb_vels = np.append(self.cb_vels, np.array([[vel.x, vel.y]]),
                                 axis=0)

    def main(self):
        """ Method to manage subscriptions:
        
        - /pyx4_node/pyx4_state: to know when a waypoint is reached.
          Callback: compare the local position in the test data for that
                    waypoint to the mavros/local_position data.
       
        - mavros/local_position/pose: receive the local position
          Callback: update the attribute self.current_pos

        - mavros/setpoint_raw_local: receive the target setpoint
          Callback: add the setpoint bitmask to self.type_masks, 
                    which contains a counter of each type mask for each wpt.

        - mavros/local_position/velocity_local: receive the velocity
          Callbacl: add the velocity to a self.cb_vels that is an array
                    of velocities
        """
        # Subscribe to pyx4_state
        rospy.Subscriber("pyx4_node/pyx4_state", Pyx4_msg,
                         self.pyx4_callback)
        # Subscribe to mavros/local_position/pose
        rospy.Subscriber("mavros/local_position/pose", PoseStamped,
                         self.local_position_callback)
        # Subscribe to mavros/setpoint_raw/local
        rospy.Subscriber('mavros/setpoint_raw/local', PositionTarget,
                        self.position_target_callback)
        # Subscribe to mavros/local_position/velocity_local
        rospy.Subscriber("mavros/local_position/velocity_local", TwistStamped,
                         self.local_position_vel_callback)
        
        rospy.init_node(NAME, anonymous=True)
        # TODO: Set proper time
        timeout_t = time.time() + 10.0*1000 #10 seconds
        while not rospy.is_shutdown() and time.time() < timeout_t:
            time.sleep(0.1)
        
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="ROS test node")
    parser.add_argument('--mission', type=str, default='basic_test.csv')
    parser.add_argument('--comp', type=str, default='basic_test.csv')
    args = parser.parse_args(rospy.myargv(argv=sys.argv)[1:])
    
    # Mission and comparisson files have the same name by definition
    comp_file = os.path.join(TEST_COMP, args.comp)
    mission_file = os.path.join(MISSION_SPECS, args.mission)

    if not os.path.isfile(mission_file):
        raise AttributeError("""Mission file {} not found.
        """.format(mission_file))

    if not os.path.isfile(comp_file):
        raise AttributeError("""file {} does not exist.
        Run test_data to create the test data for the selected mission.
        """.format(comp_file))
    
    pyx4_test = Pyx4Test(mission_file, comp_file)
    pyx4_test.main()

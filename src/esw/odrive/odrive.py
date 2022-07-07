#!/usr/bin/env python3

""" Controls an ODrive motor controller to drive the wheels.


The ODrives codebase deals with controlling the ODrive motor controllers to
drive the wheels. For the 2022 Mars Rover Rosie, there are 3 ODrive motor
controllers to drive the wheels. Each ODrive connects to the Jetson via USB.
Each ODrive has two axes and each axis is assigned a wheel. To simplify wiring
and code, the axis of each ODrive are either all left or all right. Also, the
wheels that are connected to one singular ODrive are opposite each other (i.e.
an ODrive is assigned either the front, middle, or back wheels).
"""
import sys
import threading
import time as t
from enum import Enum
from typing import Any

import fibre
import odrive as odv
import rospy
from mrover.msg import DriveStateData, DriveVelCmd, DriveVelData
from odrive.enums import (AXIS_STATE_CLOSED_LOOP_CONTROL, AXIS_STATE_IDLE,
                          CONTROL_MODE_VELOCITY_CONTROL)
from odrive.utils import dump_errors


class DisconnectedError(Exception):
    """
    An exception thrown when the Jetson is unable to talk with the ODrive.
    """
    pass


class ODriveEvent(Enum):
    """
    Dictates the behavior of the changing of States.

    The ODriveEvents are the following: When the ODrive is disconnected,
    trying to arm, or if there is an error.
    """
    DISCONNECTED_ODRIVE_EVENT = 0
    ARM_CMD_EVENT = 1
    ODRIVE_ERROR_EVENT = 2


class Modrive:
    """
    Abstracts the behavior of the ODrive.

    The ODriveBridge creates the same Modrive object
    every time it tries to connect to an ODrive. The Modrive object
    has functions that allows it to get encoder data, get state data,
    set velocity, and much more.

    :param _axes: A dictionary that maps left or right to an ODrive axes
        object.
    :param _axis_vel_cmd_mult_map: A dictionary that maps left or right to the
        vel cmd multiplier that converts [0, 1] to [0, vel_cmd_mult] turns per
        second.
    :param _axis_vel_est_mult_map: A dictionary that maps left or right to a
        multipler that converts turns/sec to m/sec.
    :param _odrive: An ODrive object
    :param _usb_lock: A lock that prevents multiple threads from accessing
        ODrive objects simultaneously.
    :param _watchdog_timeout: A float that represents the watchdog timeout.
    """
    _axes: dict[str, Any]
    _axis_vel_cmd_mult_map: dict[str, float]
    _axis_vel_est_mult_map: dict[str, float]
    _odrive: Any
    _usb_lock = threading.Lock
    _watchdog_timeout: float

    def __init__(self, odr) -> None:
        self._odrive = odr
        axis_map: dict[int, str] = {
            rospy.get_param("/odrive/axis/left"): 'left',
            rospy.get_param("/odrive/axis/right"): 'right'}

        self._axes = {
            axis_map[0]: self._odrive.axis0,
            axis_map[1]: self._odrive.axis1
        }

        self._axis_vel_cmd_mult_map = {
            'left': rospy.get_param(
                "/odrive/multiplier/vel_cmd_multiplier_left"),
            'right': rospy.get_param(
                "/odrive/multiplier/vel_cmd_multiplier_right")}

        self._axis_vel_est_mult_map = {
            'left': rospy.get_param(
                "/odrive/multiplier/vel_est_multiplier_left"),
            'right': rospy.get_param(
                "/odrive/multiplier/vel_est_multiplier_right")}
        self._usb_lock = threading.Lock()
        self._watchdog_timeout = rospy.get_param(
            "/odrive/config/watchdog_timeout")

    def __getattr__(self, attr: Any) -> Any:
        if attr in self.__dict__:
            return getattr(self, attr)
        return getattr(self._odrive, attr)

    def arm(self) -> None:
        """
        Arms the ODrive by setting it to closed loop control and
        velocity control.
        """
        self._set_closed_loop_ctrl()
        self._set_velocity_ctrl()

    def disarm(self) -> None:
        """
        Disarms the ODrive by setting the velocity to zero and making
        it idle.
        """
        self._set_closed_loop_ctrl()
        self._set_velocity_ctrl()
        self.set_vel('left', 0.0)
        self.set_vel('right', 0.0)
        self._idle()

    def get_measured_current(self, axis: str) -> float:
        """
        Returns the measured current of the requested axis of the ODrive in
        Amperes.

        :param axis: A string that represents which wheel to read current from.
            The string must be "left" or "right"
        :returns: A float that is the measured current of the corresponding
            ODrive axis in Amperes.
        :raises DisconnectedError: If Jetson is unable to communicate with
            ODrive.
        """
        assert axis == "left" or axis == "right", (
            'axis must be "left" or "right"'
        )
        try:
            self._usb_lock.acquire()
            measured_current = \
                self._axes[axis].motor.current_control.Iq_measured
        except fibre.protocol.ChannelBrokenException:
            raise DisconnectedError
        finally:
            self._usb_lock.release()
        return measured_current

    def get_vel_estimate_m_s(self, axis: str) -> float:
        """
        Returns the estimated velocity of the requested wheel of the ODrive
        in meters per second.
        :param axis: A string that represents which wheel to read current from.
            The string must be "left" or "right"
        :returns: A float that is the measured current of the corresponding
            ODrive axis in Amperes.
        :raises DisconnectedError: If Jetson is unable to communicate with
            ODrive.
        """
        assert axis == "left" or axis == "right", (
            'axis must be "left" or "right"'
        )
        try:
            self._usb_lock.acquire()
            vel_est_m_s = (
                self._axes[axis].encoder.vel_estimate
                * self._axis_vel_est_mult_map[axis]
            )
        except fibre.protocol.ChannelBrokenException:
            raise DisconnectedError
        finally:
            self._usb_lock.release()
        return vel_est_m_s

    def has_errors(self) -> bool:
        """
        Returns a boolean to show if there are ODrive errors.
        :returns: A boolean that is True if there are errors.
        :raises DisconnectedError: If Jetson is unable to communicate with
            ODrive.
        """
        try:
            self._usb_lock.acquire()
            errors = self._axes['left'].error + self._axes['right'].error != 0
        except fibre.protocol.ChannelBrokenException:
            raise DisconnectedError
        finally:
            self._usb_lock.release()
        return errors

    def reset_watchdog(self) -> None:
        """
        Resets the watchdog of the ODrives.
        This function is usually called if the ODrive has previously errored
        out or was disconnected.
        """
        print("Resetting watchdog")
        self._disable_watchdog()
        self._reset_errors()
        self._enable_watchdog()

    def set_current_lim(self, lim: float) -> None:
        """
        Sets the current limit of each ODrive axis.
        :param lim: A float that is the requested current limit.
        :raises DisconnectedError: If Jetson is unable to communicate with
            ODrive.
        """
        try:
            self._usb_lock.acquire()
            for axis in self._axes.values():
                axis.motor.config.current_lim = lim
        except fibre.protocol.ChannelBrokenException:
            raise DisconnectedError
        finally:
            self._usb_lock.release()

    def set_vel(self, axis: str, vel: float) -> None:
        """
        Sets the requested ODrive axis to run the motors at the requested
        velocity.
        :param axis: A string that represents which wheel to read current from.
        The string must be "left" or "right".
        :param vel: A float that is the requested velocity that is in range
            [-1.0, 1.0].
        :raises DisconnectedError: If Jetson is unable to communicate with
            ODrive.
        """
        assert -1.0 <= vel and vel <= 1.0, 'vel must be in range [-1.0, 1.0]'
        assert axis == "left" or axis == "right", (
            'axis must be "left" or "right"'
        )
        try:
            desired_input_vel_turns_s = vel * self._axis_vel_cmd_mult_map[axis]
            self._usb_lock.acquire()
            self._axes[axis].controller.input_vel = desired_input_vel_turns_s
        except fibre.protocol.ChannelBrokenException:
            raise DisconnectedError
        finally:
            self._usb_lock.release()

    def watchdog_feed(self) -> None:
        """
        Refreshes the ODrive watchdog feed.
        """
        try:
            self._usb_lock.acquire()
            for axis in self._axes.values():
                axis.watchdog_feed()
        except fibre.protocol.ChannelBrokenException:
            print("Failed in watchdog_feed. Unplugged")
            raise DisconnectedError
        finally:
            self._usb_lock.release()

    def _enable_watchdog(self) -> None:
        """
        Enables the ODrive watchdog.
        :raises DisconnectedError: If Jetson is unable to communicate with
            ODrive.
        """
        try:
            self._usb_lock.acquire()
            for axis in self._axes.values():
                axis.config.watchdog_timeout = self._watchdog_timeout
                axis.watchdog_feed()
                axis.config.enable_watchdog = True
        except fibre.protocol.ChannelBrokenException:
            print("Failed in _enable_watchdog. Unplugged")
            raise DisconnectedError
        finally:
            self._usb_lock.release()

    def _disable_watchdog(self) -> None:
        """
        Disables the ODrive watchdog.
        :raises DisconnectedError: If Jetson is unable to communicate with
            ODrive.
        """
        try:
            self._usb_lock.acquire()
            for axis in self._axes.values():
                axis.config.watchdog_timeout = 0
                axis.config.enable_watchdog = False
        except fibre.protocol.ChannelBrokenException:
            print("Failed in _disable_watchdog. Unplugged")
            raise DisconnectedError
        finally:
            self._usb_lock.release()

    def _idle(self) -> None:
        """
        Sets the ODrive state to idle.
        """
        self._set_requested_state(AXIS_STATE_IDLE)

    def _reset_errors(self) -> None:
        """
        Resets the errors cleanly for all axes.
        """
        try:
            self._usb_lock.acquire()
            for axis in self._axes.values():
                axis.error = 0
        except fibre.protocol.ChannelBrokenException:
            raise DisconnectedError
        finally:
            self._usb_lock.release()

    def _set_closed_loop_ctrl(self) -> None:
        """
        Sets the ODrive state to closed loop control.
        """
        self._set_requested_state(AXIS_STATE_CLOSED_LOOP_CONTROL)

    def _set_control_mode(self, mode: Any) -> None:
        """
        Sets the ODrive control mode to the requested control mode.
        :param mode: A control mode that is the requested ODrive control mode.
        :raises DisconnectedError: If Jetson is unable to communicate with
            ODrive.
        """
        try:
            self._usb_lock.acquire()
            for axis in self._axes.values():
                axis.controller.config.control_mode = mode
        except fibre.protocol.ChannelBrokenException:
            raise DisconnectedError
        finally:
            self._usb_lock.release()

    def _set_requested_state(self, state: Any) -> None:
        """
        Sets the ODrive state to the requested state.
        :param state: A state that is the requested ODrive state.
        :raises DisconnectedError: If Jetson is unable to communicate with
            ODrive.
        """
        try:
            self._usb_lock.acquire()
            for axis in self._axes.values():
                axis.requested_state = state
        except fibre.protocol.ChannelBrokenException:
            raise DisconnectedError
        finally:
            self._usb_lock.release()

    def _set_velocity_ctrl(self) -> None:
        """
        Sets the ODrive to velocity control.
        """
        self._set_control_mode(CONTROL_MODE_VELOCITY_CONTROL)


class State(object):
    """
    Provides some utility functions for the individual states within the
    state machine.
    """

    def __init__(self) -> None:
        print('Processing current state:', str(self))

    def on_event(self, event: ODriveEvent, modrive: Modrive) -> None:
        """
        Handles events that are delegated to this State.
        """

    def __repr__(self) -> None:
        """
        Makes it so __str__ method can describe the State.
        """
        return self.__str__()

    def __str__(self) -> None:
        """
        Returns the name of the State.
        State state
        str(state) = State
        """
        return self.__class__.__name__


class ArmedState(State):
    """
    Reveals that the ODrive is armed.
    """
    def on_event(self, event: ODriveEvent, modrive: Modrive) -> None:
        """
        Handles events that are delegated to the Armed State.
        """
        if event == ODriveEvent.DISCONNECTED_ODRIVE_EVENT:
            return DisconnectedState()
        elif event == ODriveEvent.ODRIVE_ERROR_EVENT:
            return ErrorState()
        return self


class DisconnectedState(State):
    """
    Reveals that the ODrive has disconnected.
    """
    def on_event(self, event: ODriveEvent, modrive: Modrive) -> None:
        """
        Handles events that are delegated to the Disconnected State.
        """
        if event == ODriveEvent.ARM_CMD_EVENT:
            modrive.disarm()
            modrive.reset_watchdog()
            modrive.arm()
            return ArmedState()
        return self


class ErrorState(State):
    """
    Reveals that the ODrive is receiving errors.
    """
    def on_event(self, event: ODriveEvent, modrive: Modrive) -> None:
        """
        Handles events that are delegated to the Error State.
        """
        print(dump_errors(modrive.odrive, True))
        if event == ODriveEvent.ODRIVE_ERROR_EVENT:
            return DisconnectedState()
        return self


class ODriveBridge(object):
    """
    Controls the behavior of one ODrive. It manages the ODrive state and
    various other behavior.

    TODO - param stuff
    :param _current_lim: A float that is the current limit in Amperes.
    :param str: A string that is the current ODrive's ID.
    :param _left_speed: A float that is the requested left wheel speed.
    :param _modrive: A Modrive object that abstracts away the ODrive functions.
    :param _pair: A string that is front, middle, or back.
    :param _right_speed: A float that is the requested right wheel speed.
    :param _speed_lock: A lock that prevents multiple threads from accessing
        self._left_speed and self._right_speed simultaneously.
    :param _start_time: An object that helps keep track of time for the
        watchdog.
    :param _state: A State object that keeps track of the current state.
    :param _state_pub: A rospy Publisher object that is used for publishing
        state data.
    :param _vel_pub: A rospy Publisher object that is used for publishing
        velocity data.
    """
    _current_lim: float
    _id: str
    _left_speed: float
    _modrive: Modrive
    _pair: str
    _right_speed: float
    _speed_lock: threading.Lock
    _start_time: t.clock
    _state: State
    _state_pub: rospy.Publisher
    _vel_pub: rospy.Publisher

    def __init__(self) -> None:
        """
        Initializes the components, starting with a Disconnected State.
        """
        self._current_lim = rospy.get_param("/odrive/config/current_lim")
        self._left_speed = 0.0
        self._modrive = None
        self._right_speed = 0.0
        _odrive_ids = {
            'front': rospy.get_param("/odrive/ids/front"),
            'middle': rospy.get_param("/odrive/ids/middle"),
            'back': rospy.get_param("/odrive/ids/back")}
        self._pair = sys.argv[1]
        self._id = _odrive_ids[self._pair]
        self._speed_lock = threading.Lock()
        self._start_time = t.clock()
        self._state = DisconnectedState()
        self._state_pub = rospy.Publisher(
            'drive_state_data', DriveStateData, queue_size=1)
        self._vel_pub = rospy.Publisher(
            'drive_vel_data', DriveVelData, queue_size=1)

    def drive_vel_cmd_callback(self, ros_msg: DriveVelCmd) -> None:
        """
        Calls the change speeds function.

        Note that this does NOT actually change speed that the ODrive comands
        the motors at. One must wait for the ODriveBridge._update() function
        to be called for that to happen.
        :param ros_msg: A ROS message that has two floats that represents the
            requested speeds for the left and right wheels.
        """
        if self._get_state_string() == "Armed":
            ros_msg.left = self._throttle(ros_msg.left)
            ros_msg.right = self._throttle(ros_msg.right)
            self._change_speeds(ros_msg.left, ros_msg.right)

    def ros_publish_data_loop(self) -> None:
        """
        Publishes velocity and current data continuously.
        """
        while not rospy.is_shutdown():
            self._start_time = t.clock()
            self._publish_encoder_msg()

    def watchdog_while_loop(self) -> None:
        """
        Calls the update() function continuously and checks if comms has
        been lost.
        """
        # flag for state when we have comms with base_station vs not
        previously_lost_comms = True
        while not rospy.is_shutdown():
            watchdog = t.clock() - self._start_time
            lost_comms = watchdog > 1.0
            if lost_comms:
                if not previously_lost_comms:
                    # done to print "loss of comms" once
                    print("loss of comms")
                    previously_lost_comms = True

                self._change_speeds(0.0, 0.0)
            elif previously_lost_comms:
                previously_lost_comms = False
                print("regained comms")

            try:
                self._update()
            except DisconnectedError:
                self._bridge_on_event(
                    ODriveEvent.DISCONNECTED_ODRIVE_EVENT
                )

    def _bridge_on_event(self, event: ODriveEvent) -> None:
        """
        Delegates incoming events to the given states which then handle
        the event. The result is then assigned as the new state.

        Note that this does NOT actually change speed that the ODrive comands
        the motors at. One must wait for the ODriveBridge._update() function
        to be called for that to happen.
        :param event: An ODriveEvent that determines the behavior of the
            switching of states.
        """
        try:
            print("on event called, ODrive event:", event)
            self._state = self._state.on_event(event, self._modrive)
            self._publish_state_msg(self._get_state_string())
        except DisconnectedError:
            return

    def _change_speeds(self, left: float, right: float) -> None:
        """
        Sets the self._left_speed and self._right_speed to the requested
        speeds. The speeds must be in the range [-1.0, 1.0].
        :param left: A float representing the requested left wheel speed.
        :param right: A float representing the requested right wheel speed.
        """
        assert -1.0 <= left and left <= 1.0, (
            'left must be in range[-1.0, 1.0]'
        )
        assert -1.0 <= right and right <= 1.0, (
            'right must be in range [-1.0, 1.0]'
        )
        self._speed_lock.acquire()
        self._left_speed = left
        self._right_speed = right
        self._speed_lock.release()

    def _connect(self) -> None:
        """
        Connects to an ODrive if one is connected.

        This uses the ODrive library to look for an ODrive by its ID.
        """
        print("looking for ODrive")
        odrive_id = self._id
        print(odrive_id)
        odrive = odv.find_any(serial_number=odrive_id)
        print("found odrive")
        self._modrive = Modrive(odrive)
        self._modrive.set_current_lim(self._current_lim)

    def _get_state_string(self) -> str:
        """
        Returns the state of the ODriveBridge as a short string.

        The form is of "[insert_odrive_state]State". e.g. state is ErrorState,
        so ros_msg.state is Error
        :returns: A string that is the state of the ODriveBridge.
        """
        state = str(self._state)
        return state[:len(state) - len("State")]

    def _publish_encoder_helper(self, axis: str) -> None:
        """
        Publishes the velocity and current data of the requested axis to a ROS
        topic.
        :param axis: A string that represents which wheel to read current from.
            The string must be "left" or "right"
        """
        assert axis == "left" or axis == "right", (
            'axis must be "left" or "right"'
        )
        ros_msg = DriveVelData()
        try:
            ros_msg.current_amps = self._modrive.get_measured_current(axis)
            ros_msg.vel_m_s = self._modrive.get_vel_estimate_m_s(axis)
            # e.g. "back_left" or "middle_right" or "front_left"
            ros_msg.wheel = f'{self.pair}_{axis}'
            self._vel_pub.publish(ros_msg)
        except DisconnectedError:
            return

    def _publish_encoder_msg(self) -> None:
        """
        Publishes the velocity and current data of all the axes to a ROS topic.
        """
        self._publish_encoder_helper('left')
        self._publish_encoder_helper('right')

    def _publish_state_msg(self, state: str) -> None:
        """
        Publishes the ODrive state message over ROS to a topic.
        :param state: A string that is the current state.
        """
        ros_msg = DriveStateData()
        ros_msg.state = state
        ros_msg.pair = self._pair
        self._state_pub.publish(ros_msg)
        print(f'The state is now {state}')

    def _throttle(self, speed: float) -> float:
        """
        Throttles the speed to a range of [-1.0, 1.0].
        :param speed: A float that is the input speed.
        :returns: A float that limits the input to the range [-1.0, 1.0].
        """
        speed = max(-1.0, speed)
        speed = min(speed, 1.0)
        return speed

    def _update(self) -> None:
        """
        Updates based on the current state.

        In the armed state, it will first check for ODrive errors. Then, it
        will _update the speed. In the disconnected state, it will create an
        arm event. In the error state, it will create an error event.
        """
        if str(self._state) == "ArmedState":
            try:
                errors = self._modrive.has_errors()
                self._modrive.watchdog_feed()
            except DisconnectedError:
                errors = 0
                self._bridge_on_event(ODriveEvent.DISCONNECTED_ODRIVE_EVENT)
                return

            if errors:
                self._bridge_on_event(ODriveEvent.ODRIVE_ERROR_EVENT)
                return

            self._speed_lock.acquire()
            left_speed = self._left_speed
            right_speed = self._right_speed
            self._speed_lock.release()
            self._modrive.set_vel('left', left_speed)
            self._modrive.set_vel('right', right_speed)

        elif str(self._state) == "DisconnectedState":
            self._connect()
            self._bridge_on_event(ODriveEvent.ARM_CMD_EVENT)

        elif str(self._state) == "ErrorState":
            self._bridge_on_event(ODriveEvent.ODRIVE_ERROR_EVENT)


def main():
    """
    Runs the ros_publish_data_loop, rospy drive_vel_cmd subscriber thread,
    and watchdog_while_loop all run simultaneously.
    """
    rospy.init_node(f"odrive_{int(sys.argv[1])}")

    bridge = ODriveBridge()

    # Starts the thread to listen to drive commands
    rospy.Subscriber("drive_vel_cmd", DriveVelCmd,
                     bridge.drive_vel_cmd_callback)

    # Starts the thread to listen to continuously publish data
    threading._start_new_thread(bridge.ros_publish_data_loop, ())

    # Starts the thread to continuously monitor comms between
    # basestation and jetson to act as watchdog.
    threading._start_new_thread(bridge.watchdog_while_loop, ())
    rospy.spin()
    exit()


if __name__ == "__main__":
    main()
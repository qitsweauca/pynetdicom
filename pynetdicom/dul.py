"""
Implements the DICOM Upper Layer service provider.
"""

import logging
try:
    import queue
except ImportError:
    import Queue as queue  # Python 2 compatibility
import socket
from struct import unpack
import struct
from threading import Thread
import time

from pynetdicom.fsm import StateMachine
from pynetdicom.pdu import (
    A_ASSOCIATE_RQ, A_ASSOCIATE_AC, A_ASSOCIATE_RJ,
    P_DATA_TF, A_RELEASE_RQ, A_RELEASE_RP, A_ABORT_RQ
)
from pynetdicom.pdu_primitives import (
    A_ASSOCIATE, A_RELEASE, A_ABORT, A_P_ABORT, P_DATA
)
from pynetdicom.timer import Timer


LOGGER = logging.getLogger('pynetdicom.dul')


class DULServiceProvider(Thread):
    """The DICOM Upper Layer Service Provider.

    Attributes
    ----------
    artim_timer : timer.Timer
        The ARTIM timer
    association : association.Association
        The DUL's current Association
    socket : transport.AssociationSocket
        A wrapped socket.socket object used to communicate with the peer.
    to_provider_queue : queue.Queue
        Queue of PDUs from the DUL service user to be processed by the DUL
        provider
    to_user_queue : queue.Queue
        Queue of primitives from the DUL service to be processed by the DUL user
    event_queue : queue.Queue
        List of queued events to be processed by the state machine
    state_machine : fsm.StateMachine
        The DICOM Upper Layer's State Machine
    """
    def __init__(self, assoc):
        """
        Parameters
        ----------
        assoc : association.Association
            The DUL's parent Association instance.
        """
        # The association thread
        self.assoc = assoc
        self.socket = None

        # Current primitive and PDU
        # TODO: Don't do it this way
        self.primitive = None
        self.pdu = None

        # Tracks the events the state machine needs to process
        self.event_queue = queue.Queue()
        # These queues provide communication between the DUL service
        #   user and the DUL service provider.
        # An event occurs when the DUL service user adds to
        #   the to_provider_queue
        self.to_provider_queue = queue.Queue()
        # A primitive is sent to the service user when the DUL service provider
        # adds to the to_user_queue.
        self.to_user_queue = queue.Queue()

        # Set the (network) idle and ARTIM timers
        # Timeouts gets set after DUL init so these are temporary
        self._idle_timer = Timer(60)
        self.artim_timer = Timer(30)

        # State machine - PS3.8 Section 9.2
        self.state_machine = StateMachine(self)

        # Controls the minimum delay between loops in run()
        # TODO: try and make this event based rather than running loops
        self._run_loop_delay = 0.001

        Thread.__init__(self)
        self.daemon = False
        self._kill_thread = False

    def _check_incoming_primitive(self):
        """Check the incoming primitive."""
        try:
            # Check the queue and see if there are any primitives
            # If so then put the corresponding event on the event queue
            self.primitive = self.to_provider_queue.get(False)
            self.event_queue.put(self._primitive_to_event(self.primitive))
            return True
        except queue.Empty:
            return False

    def _decode_pdu(self, bytestream):
        """Decode a received PDU.

        Parameters
        ----------
        bytestream : bytearray
            The received PDU.

        Returns
        -------
        pdu.PDU subclass, str
            The PDU subclass corresponding to the PDU and the event string
            corresponding to receiving that PDU type.
        """
        acse = self.assoc.acse
        pdu_types = {
            0x01 : (A_ASSOCIATE_RQ, 'Evt6', acse.debug_receive_associate_rq),
            0x02 : (A_ASSOCIATE_AC, 'Evt3', acse.debug_receive_associate_ac),
            0x03 : (A_ASSOCIATE_RJ, 'Evt4', acse.debug_receive_associate_rj),
            0x04 : (P_DATA_TF, 'Evt10', acse.debug_receive_data_tf),
            0x05 : (A_RELEASE_RQ, 'Evt12', acse.debug_receive_release_rq),
            0x06 : (A_RELEASE_RP, 'Evt13', acse.debug_receive_release_rp),
            0x07 : (A_ABORT_RQ, 'Evt16', acse.debug_receive_abort)
        }

        pdu, event, acse_callback = pdu_types[bytestream[0]]
        pdu = pdu()
        pdu.decode(bytes(bytestream))

        # ACSE callback
        acse_callback(pdu)

        return pdu, event

    def idle_timer_expired(self):
        """
        Checks if the idle timer has expired

        Returns
        -------
        bool
            True if the idle timer has expired, False otherwise.
        """
        return self._idle_timer.expired

    def _is_artim_expired(self):
        """Return if the state machine's ARTIM timer has expired.

        If it has then 'Evt18' is added to the event queue.

        Returns
        -------
        bool
            True if the ARTIM timer has expired, False otherwise
        """
        if self.artim_timer.expired:
            self.event_queue.put('Evt18')
            return True

        return False

    def _is_transport_event(self):
        """Check to see if the socket has incoming data

        Returns
        -------
        bool
            True if an event has been added to the event queue, False
            otherwise. Returning True restarts the idle timer and skips the
            incoming primitive check.
        """
        # Sta13: waiting for the transport connection to close
        # however it may still receive data that needs to be acted on
        if self.state_machine.current_state == 'Sta13':
            # Check to see if there's more data to be read
            #   Might be any incoming PDU or valid/invalid data
            if self.socket and self.socket.ready:
                # Data still available, grab it
                self._read_pdu_data()
                return True

            # Once we have no more incoming data close the socket and
            #   add the corresponding event to the queue
            self.socket.close()

            return True

        # By this point the connection should be established
        #   If theres incoming data on the connection then check the PDU
        #   type
        # Fix for #28 - caused by peer disconnecting before run loop is
        #   stopped by assoc.release()
        if self.socket and self.socket.ready:
            self._read_pdu_data()
            return True

        return False

    def kill_dul(self):
        """Immediately interrupts the thread"""
        self._kill_thread = True

    @property
    def network_timeout(self):
        """Return the network_timeout."""
        return self.assoc.network_timeout

    def peek_next_pdu(self):
        """Check the next PDU to be processed."""
        try:
            return self.to_user_queue.queue[0]
        except (queue.Empty, IndexError):
            return None

    @staticmethod
    def _primitive_to_event(primitive):
        """Returns the state machine event associated with sending a primitive.

        Parameters
        ----------
        primitive : pdu_primitives.ServiceParameter
            The Association primitive

        Returns
        -------
        str
            The event associated with the primitive
        """
        if primitive.__class__ == A_ASSOCIATE:
            if primitive.result is None:
                # A-ASSOCIATE Request
                event_str = 'Evt1'
            elif primitive.result == 0x00:
                # A-ASSOCIATE Response (accept)
                event_str = 'Evt7'
            else:
                # A-ASSOCIATE Response (reject)
                event_str = 'Evt8'
        elif primitive.__class__ == A_RELEASE:
            if primitive.result is None:
                # A-Release Request
                event_str = 'Evt11'
            else:
                # A-Release Response
                # result is 'affirmative'
                event_str = 'Evt14'
        elif primitive.__class__ in (A_ABORT, A_P_ABORT):
            event_str = 'Evt15'
        elif primitive.__class__ == P_DATA:
            event_str = 'Evt9'
        else:
            raise ValueError("_primitive_to_event(): invalid primitive")

        return event_str

    def _read_pdu_data(self):
        """Read PDU data sent by the peer from the socket.

        Receives the PDU, attempts to decode it, places the corresponding
        event in the event queue and and converts it a primitive (if possible).

        If the decoding and conversion is successful then `pdu` and `primitive`
        are set to corresponding class instances.

        **Events Emitted**

        - Evt6: A-ASSOCIATE-RQ PDU received
        - Evt3: A-ASSOCIATE-AC PDU received
        - Evt4: A-ASSOCIATE-RJ PDU received
        - Evt10: P-DATA-TF PDU received
        - Evt12: A-RELEASE-RQ PDU received
        - Evt13: A-RELEASE-RP PDU received
        - Evt16: A-ABORT PDU received
        - Evt17: Transport connection closed
        - Evt19: Invalid or unrecognised PDU
        """
        bytestream = bytearray()

        # Try and read the PDU type and length from the socket
        try:
            bytestream.extend(self.socket.recv(6))
        except (socket.error, socket.timeout):
            # Evt17: Transport connection closed
            self.event_queue.put('Evt17')
            return

        try:
            # Byte 1 is always the PDU type
            # Byte 2 is always reserved
            # Bytes 3-6 are always the PDU length
            pdu_type, _, pdu_length = unpack('>BBL', bytestream)
        except struct.error:
            # Raised if there's not enough data
            # Evt17: Transport connection closed
            self.event_queue.put('Evt17')
            return

        # If the `pdu_type` is unrecognised
        if pdu_type not in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07):
            # Evt19: Unrecognised or invalid PDU received
            self.event_queue.put('Evt19')
            return

        # Try and read the rest of the PDU
        try:
            bytestream += self.socket.recv(pdu_length)
        except (socket.error, socket.timeout):
            # Evt17: Transport connection closed
            self.event_queue.put('Evt17')
            return

        # Check that the PDU data was completely read
        if len(bytestream) != 6 + pdu_length:
            # Evt17: Transport connection closed
            self.event_queue.put('Evt17')
            return

        try:
            # Decode the PDU data, get corresponding FSM and callback events
            pdu, event = self._decode_pdu(bytestream)
            self.event_queue.put(event)
        except Exception as exc:
            LOGGER.error('Unable to decode the received PDU data')
            LOGGER.exception(exc)
            # Evt19: Unrecognised or invalid PDU received
            self.event_queue.put('Evt19')
            return

        self.pdu = pdu
        self.primitive = self.pdu.to_primitive()

    def receive_pdu(self, wait=False, timeout=None):
        """
        Get the next item to be processed out of the queue of items sent
        from the DUL service provider to the service user

        Parameters
        ----------
        wait : bool, optional
            If `wait` is True and `timeout` is None, blocks until an item
            is available. If `timeout` is a positive number, blocks at most
            `timeout` seconds. Otherwise returns an item if one is immediately
            available.
        timeout : int or None
            See the definition of `wait`

        Returns
        -------
        queue_item
            The next object in the to_user_queue.
        None
            If the queue is empty.
        """
        try:
            # Remove and return an item from the queue
            #   If block is True and timeout is None then block until an item
            #       is available.
            #   If timeout is a positive number, blocks timeout seconds and
            #       raises queue.Empty if no item was available in that time.
            #   If block is False, return an item if one is immediately
            #       available, otherwise raise queue.Empty
            queue_item = self.to_user_queue.get(block=wait, timeout=timeout)
            return queue_item
        except queue.Empty:
            return None

    def run(self):
        """
        The main threading.Thread run loop. Runs constantly, checking the
        connection for incoming data. When incoming data is received it
        categorises it and add its to the `to_user_queue`.
        """
        # Main DUL loop
        self._idle_timer.start()

        while True:
            # This effectively controls how often the DUL checks the network
            time.sleep(self._run_loop_delay)

            if self._kill_thread:
                break

            # Check the connection for incoming data
            try:
                if self._is_transport_event():
                    self._idle_timer.restart()
                elif self._check_incoming_primitive():
                    pass

                if self._is_artim_expired():
                    self._kill_thread = True

            except Exception as exc:
                LOGGER.error("Exception in DUL.run()")
                LOGGER.exception(exc)
                self._kill_thread = True
                raise

            # Check the event queue to see if there is anything to do
            try:
                event = self.event_queue.get(block=False)
            # If the queue is empty, return to the start of the loop
            except queue.Empty:
                continue

            self.state_machine.do_action(event)

    def send_pdu(self, primitive):
        """Place a primitive in the provider queue to be sent to the peer.

        Primitives are converted to the corresponding PDU and encoded before
        sending.

        Parameters
        ----------
        primitive - pdu_primitives class
            A service primitive, one of A_ASSOCIATE, A_RELEASE, A_ABORT,
            A_P_ABORT or P_DATA.
        """
        self.to_provider_queue.put(primitive)

    def stop_dul(self):
        """
        Interrupts the thread if state is "Sta1"

        Returns
        -------
        bool
            True if Sta1, False otherwise
        """
        if self.state_machine.current_state == 'Sta1':
            self._kill_thread = True
            # Fix for Issue 39
            # Give the DUL thread time to exit
            while self.is_alive():
                time.sleep(0.001)

            return True

        return False
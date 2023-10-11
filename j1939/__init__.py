"""
SAE J1939 vehicle bus standard.

SAE J1939 defines a higher layer protocol on CAN.
It implements a more sophisticated addressing scheme
and extends the maximum packet size above 8 bytes.

http://en.wikipedia.org/wiki/J1939
"""

import threading
import logging
#import logging.handlers
from logging.handlers import RotatingFileHandler
from logging import StreamHandler
import pprint
import time
import tempfile
import os, sys

try:
    from queue import Queue, Empty
except ImportError:
    from Queue import Queue, Empty

import copy

# By this stage the can.rc should have been set up
from can import CanError
from can import Message
from can import set_logging_level as can_set_logging_level
from can.interface import Bus as RawCanBus
from can.listener import Listener as canListener
from can.bus import BusABC

# Import our new message type
from j1939.pdu import PDU
from j1939.pgn import PGN
from j1939.constants import *
from j1939.notifier import Notifier, CanNotifier as canNotifier
from j1939.node import Node
from j1939.nodename import NodeName
from j1939.arbitrationid import ArbitrationID
from j1939.utils import *

__version__ = "1.0.0"
# lLevel = logging.debug
#logger = logging.getLogger("j1939")

filenname_and_path = os.path.join(tempfile.gettempdir(), 'j1939__3.log')
logging.basicConfig(filename=filenname_and_path, level=logging.DEBUG)
logger = logging.getLogger('j1939')
logger.setLevel(logging.DEBUG)
handler = RotatingFileHandler(filenname_and_path, maxBytes=(1024*1024*20), backupCount=10)
logger.addHandler(handler)

# ch = logging.StreamHandler()
# ch.setLevel(lLevel)
# chformatter = logging.Formatter('%(name)25s | %(threadName)10s | %(levelname)5s | %(message)s')
# ch.setFormatter(chformatter)
# # logger.addHandler(ch)
# print(f"Path: {os.path.join(tempfile.gettempdir(), 'j1939_debug.log')}")
#handler = logging.handlers.RotatingFileHandler(, maxBytes = (1024*1024*20), backupCount = 4)
# fileHandler.setFormatter(chformatter)
# fileHandler.setLevel(lLevel)
#logger.addHandler(handler)
# can_set_logging_level(lLevel) #'debug')

# logger = logging.getLogger("j1939")
# logger.setLevel(logging.debug)
# handler = StreamHandler(stream=sys.stdout)
# logger.addHandler(handler)


class j1939Listner(canListener):

    def __init__(self, handler):
        self.handler = handler

    def on_message_received(self, msg):
        self.handler(msg)

    def stop(self):
        pass
            

class Bus(BusABC):
    """
    A CAN Bus that implements the J1939 Protocol.

    :param list j1939_filters:
        a list of dictionaries that specify filters that messages must
        match to be received by this Bus. Messages can match any of the
        filters.

        Options are:

        * :pgn: An integer PGN to show
    """

    channel_info = "j1939 bus"

    def __init__(self, pdu_type=PDU, broadcast=True, *args, **kwargs):
        logging.debug("!!Creating a new j1939 bus!!")
        logging.debug(f"kwargs: {kwargs}")

        #self.rx_can_message_queue = Queue()

        self.queue = Queue()
        self.node_queue_list = []  # Start with nothing

        super(Bus, self).__init__(kwargs.get('channel'), kwargs.get('can_filters'))
        self._pdu_type = pdu_type
        self.timeout = 1
        self._long_message_throttler = threading.Thread(target=self._throttler_function)
        self._long_message_throttler.daemon = True

        self._incomplete_received_pdus = {}
        self._incomplete_received_pdu_lengths = {}
        self._incomplete_transmitted_pdus = {}
        self._long_message_segment_queue = Queue(0)
        self._key_generation_fcn = None
        self._ignore_can_send_error = False

        self._key_generation_fcn = kwargs.get('keygen')
        logging.debug("----PI01d: self._key_generation_fcn={}".format(self._key_generation_fcn))


        self._ignore_can_send_error = kwargs.get('ignoreCanSendError')

        if broadcast:
            self.node_queue_list = [(None,  self)]  # Start with default logger Queue which will receive everything

        # Convert J1939 filters into Raw Can filters

        if 'j1939_filters' in kwargs and kwargs['j1939_filters'] is not None:
            filters = kwargs.pop('j1939_filters')
            logging.debug("Got filters: {}".format(filters))
            can_filters = []
            for filt in filters:
                can_id, can_mask = 0, 0
                if 'pgn' in filt:
                    can_id = filt['pgn'] << 8
                    # The pgn needs to be left shifted by 8 to ignore the CAN_ID's source address
                    # Look at most significant 4 bits to determine destination specific
                    if can_id & 0xF00000 == 0xF00000:
                        logging.info("PDU2 (broadcast message)")
                        can_mask = 0xFFFF00
                    else:
                        logging.info("PDU1 (p2p)")
                        can_mask = 0xFF0000
                if 'source' in filt:
                    # filter by source
                    can_mask |= 0xFF
                    can_id |= filt['source']
                    logging.debug("added source", filt)

                logging.debug("Adding CAN ID filter: {:0x}:{:0x}".format(can_id, can_mask))
                can_filters.append({"can_id": can_id, "can_mask": can_mask})
            kwargs['can_filters'] = can_filters

        if 'timeout' in kwargs and kwargs['timeout'] is not None:
            if isinstance(kwargs['timeout'], (int, float)):
                self.timeout = kwargs['timeout']
            else:
                raise ValueError("Bad timeout type")

        logging.debug("Creating a new can bus")
        self.can_bus = RawCanBus(*args, **kwargs)

        canListener = j1939Listner(self.notification)
        self.can_notifier = canNotifier(self.can_bus, [canListener], timeout=self.timeout)

        self._long_message_throttler.start()


    def notification(self, inboundMessage):
        #self.rx_can_message_queue.put(inboundMessage)
        if self.can_notifier._running is False:
            logging.debug('{}: Aborting message {} bus is not running'.format(inspect.stack()[0][3], inboundMessage))
            # Should I return or throw exception here.

        if isinstance(inboundMessage, Message):
            logging.debug('\n\n{}:  Got a Message from CAN: {}'.format(inspect.stack()[0][3],inboundMessage))
            if inboundMessage.is_extended_id:
                # Extended ID
                # Only J1939 messages (i.e. 29-bit IDs) should go further than this point.
                # Non-J1939 systems can co-exist with J1939 systems, but J1939 doesn't care
                # about the content of their messages.
                logging.debug('{}: Message is j1939 msg'.format(inspect.stack()[0][3]))

                #
                # Need to determine if it's a broadcast message or
                # limit to listening nodes only
                #
                arbitration_id = ArbitrationID()
                arbitration_id.can_id = inboundMessage.arbitration_id
                logging.debug("{}: ArbitrationID = {}, inboundMessage.arbitration_id: 0x{:08x}".format(inspect.stack()[0][3],arbitration_id, inboundMessage.arbitration_id))

                for (node, l_notifier) in self.node_queue_list:
                    logging.debug("notification: node=%s" % (node))
                    logging.debug("              notifier=%s" % (l_notifier))
                    logging.debug("              arbitration_id.pgn=%s" % (arbitration_id.pgn))
                    logging.debug("              destination_address=%s" % (arbitration_id.destination_address))

                    # redirect the AC stuff to the node processors. the rest can go
                    # to the main queue.
                    if node and (arbitration_id.pgn in [PGN_AC_ADDRESS_CLAIMED, PGN_AC_COMMANDED_ADDRESS, PGN_REQUEST_FOR_PGN]):
                        logging.debug("{}: sending to notifier queue".format(inspect.stack()[0][3]))
                        # send the PDU to the node processor.
                        l_notifier.queue.put(inboundMessage)

                    # if node has the destination address, do something with the PDU
                    elif node and (arbitration_id.destination_address in node.address_list):
                        logging.debug("{}: sending to process_incoming_message".format(inspect.stack()[0][3]))
                        rx_pdu = self._process_incoming_message(inboundMessage)
                        if rx_pdu:
                            logging.debug("WP02: notification: sent to general queue: %s QQ=%s" % (rx_pdu, self.queue))
                            self.queue.put(rx_pdu)
                    elif node and (arbitration_id.destination_address is None):
                        logging.debug("{}: sending broadcast to general queue".format(inspect.stack()[0][3]))
                        rx_pdu = self._process_incoming_message(inboundMessage)
                        logging.debug("WP01: notification: sent broadcast to general queue: %s QQ=%s" % (rx_pdu, self.queue))
                        self.queue.put(rx_pdu)
                    elif node is None:
                        # always send the message to the logging queue
                        logging.debug("{}: sending to general queue".format(inspect.stack()[0][3]))
                        rx_pdu = self._process_incoming_message(inboundMessage)
                        logging.debug("WP03: notification: sent pdu [%s] to general queue" % rx_pdu)
                        self.queue.put(rx_pdu)
                    else:
                        logging.debug("WP04: notification: pdu dropped: %s\n\n" % inboundMessage)
            else:
                logging.debug("Received non J1939 message (ignoring)")

    def connect(self, node):
        """
        Attach a listening node (with a dest address) to the J1939 bus
        """
        logging.debug("connect: type(node)=%s, node=%s" % (type(node), node))
        if not isinstance(node, Node):
            raise ValueError("bad parameter for node, must be a J1939 node object")

        notifier = Notifier(Queue(), node.on_message_received, timeout=None)
        self.node_queue_list.append((node, notifier))

    def recv(self, timeout=None):
        #logging.debug("Waiting for new message")
        #logging.debug("Timeout is {}".format(timeout))
        logging.debug('J1939 Bus recv(), waiting on QQ=%s with timeout %s' % (self.queue, timeout))
        try:
            #m = self.rx_can_message_queue.get(timeout=timeout)
            rx_pdu = self.queue.get(timeout=timeout)
            logging.debug('J1939 Bus recv() successful QQ=%s, pdu:%s' % (self.queue, rx_pdu))
            return rx_pdu

        except Empty:
            logging.debug('J1939 Bus recv() timed out' % ())
            return None

        # TODO: Decide what to do with CAN errors
        # if m.is_error_frame:
        #     logging.debug("Appears we got an error frame!")
        #
        #     rx_error = CANError(timestamp=m.timestamp)
        #     if rx_error is not None:
        #          logging.debug('Sending error "%s" to registered listeners.' % rx_error)
        #          for listener in self.listeners:
        #              if hasattr(listener, 'on_error_received'):
        #                  listener.on_error_received(rx_error)

    def send(self, msg, timeout=None):
        logging.debug("j1939.send: msg={}".format(msg))
        messages = []
        if len(msg.data) > 8:
            logging.debug("j1939.send: message is > than 8 bytes")
            # Making a copy of the PDU so that the original
            # is not altered by the data padding.
            pdu = copy.deepcopy(msg)
            pdu.data = bytearray(pdu.data)

            logging.debug("j1939.send: Copied msg = {}".format(pdu))
            pdu_length_lsb, pdu_length_msb = divmod(len(pdu.data), 256)

            while len(pdu.data) % 7 != 0:
                pdu.data += b'\xFF'

            logging.debug("j1939.send: padded msg (mod 7) = %s" % pdu)
            logging.debug("MIL8:---------------------")

            # 
            # segment the longer message into 7 byte segments.  We need to prefix each 
            # data[0] with a sequence number for the transfer
            #
            for i, segment in enumerate(pdu.data_segments(segment_length=7)):
                arbitration_id = copy.deepcopy(pdu.arbitration_id)
                arbitration_id.pgn.value = PGN_TP_DATA_TRANSFER

                logging.debug("MIL8: j1939.send: i=%d, pdu.arbitration_id.pgn.is_destination_specific=%d, data=%s" % 
                            (i,pdu.arbitration_id.pgn.is_destination_specific, segment))

                if pdu.arbitration_id.pgn.is_destination_specific and \
                   pdu.arbitration_id.destination_address != DESTINATION_ADDRESS_GLOBAL:

                    arbitration_id.pgn.pdu_specific = pdu.arbitration_id.pgn.pdu_specific
                else:
                    arbitration_id.pgn.pdu_specific = DESTINATION_ADDRESS_GLOBAL
                    arbitration_id.destination_address = DESTINATION_ADDRESS_GLOBAL

                logging.debug("MIL8: j1939.send: segment=%d, arb = %s" % (i, arbitration_id))
                message = Message(arbitration_id=arbitration_id.can_id,
                                  is_extended_id=True,
                                  dlc=(len(segment) + 1),
                                  data=(bytearray([i + 1]) + segment))
                messages.append(message)

            #
            # At this point we have the queued messages sequenced in 'messages'
            #
            logging.debug("MIL8: j1939.send: is_destination_specific={}, destAddr={}".format(pdu.arbitration_id.pgn.is_destination_specific, pdu.arbitration_id.destination_address))
            logging.debug("MIL8: j1939.send: messages=%s" % messages)
           
            if pdu.arbitration_id.pgn.is_destination_specific and \
               pdu.arbitration_id.destination_address != DESTINATION_ADDRESS_GLOBAL:

                destination_address = pdu.arbitration_id.pgn.pdu_specific

                if pdu.arbitration_id.source_address in self._incomplete_transmitted_pdus:
                    if destination_address in self._incomplete_transmitted_pdus[pdu.arbitration_id.source_address]:
                        logging.debug("Duplicate transmission of PDU:\n{}".format(pdu))
                else:
                    self._incomplete_transmitted_pdus[pdu.arbitration_id.source_address] = {}

                # append the messages to the 'incomplete' list
                self._incomplete_transmitted_pdus[pdu.arbitration_id.source_address][destination_address] = messages

            else:
                destination_address = DESTINATION_ADDRESS_GLOBAL

            logging.debug("MIL8: rts arbitration id: src=%s, dest=%s" % (pdu.source, destination_address))
            rts_arbitration_id = ArbitrationID(pgn=PGN_TP_CONNECTION_MANAGEMENT, source_address=pdu.source, destination_address=destination_address)
            rts_arbitration_id.pgn.pdu_specific = pdu.arbitration_id.pgn.pdu_specific

            temp_pgn = copy.deepcopy(pdu.arbitration_id.pgn)
            if temp_pgn.is_destination_specific:
                temp_pgn.value -= temp_pgn.pdu_specific

            pgn_msb = ((temp_pgn.value & 0xFF0000) >> 16)
            pgn_middle = ((temp_pgn.value & 0x00FF00) >> 8)
            pgn_lsb = (temp_pgn.value & 0x0000FF)

            if pdu.arbitration_id.pgn.is_destination_specific and \
               pdu.arbitration_id.destination_address != DESTINATION_ADDRESS_GLOBAL:
                # send request to send
                logging.debug("MIL8: rts to specific dest: src=%s, dest=%s" % (pdu.source, destination_address))
                rts_msg = Message(is_extended_id=True,
                                  arbitration_id=rts_arbitration_id.can_id,
                                  data=[CM_MSG_TYPE_RTS,
                                        pdu_length_msb,
                                        pdu_length_lsb,
                                        len(messages),
                                        0xFF,
                                        pgn_lsb,
                                        pgn_middle,
                                        pgn_msb],
                                  dlc=8)
                try:
                    logging.debug("MIL08: j1939.send: sending TP.RTS to %s: %s" % (destination_address, rts_msg))
                    self.can_bus.send(rts_msg)
                except CanError:
                    if self._ignore_can_send_error:
                        pass
                    raise
            else:
                rts_arbitration_id.pgn.pdu_specific = DESTINATION_ADDRESS_GLOBAL
                rts_arbitration_id.destination_address = DESTINATION_ADDRESS_GLOBAL
                logging.debug("MIL8: rts to Global dest: src=%s, dest=%s" % (pdu.source, destination_address))
                bam_msg = Message(is_extended_id=True,
                                  arbitration_id=rts_arbitration_id.can_id | pdu.source,
                                  data=[CM_MSG_TYPE_BAM,
                                        pdu_length_msb,
                                        pdu_length_lsb, len(messages),
                                        0xFF,
                                        pgn_lsb,
                                        pgn_middle,
                                        pgn_msb],
                                  dlc=8)
                bam_msg.destination_address = DESTINATION_ADDRESS_GLOBAL
                # bam_msg.arbitration_id.destination_address = DESTINATION_ADDRESS_GLOBAL
                # send BAM
                try:
                    logging.debug("j1939.send: sending TP.BAM to %s: %s" % (destination_address, bam_msg))
                    self.can_bus.send(bam_msg)
                    time.sleep(0.05)
                except CanError:
                    if self._ignore_can_send_error:
                        pass
                    raise

                for message in messages:
                    # send data messages - no flow control, so no need to wait
                    # for receiving devices to acknowledge
                    logging.debug("j1939.send: queue TP.BAM data to %s: %s" % (destination_address, message))
                    self._long_message_segment_queue.put_nowait(message)
        else:
            msg.display_radix = 'hex'
            logging.debug("j1939.send: calling can_bus_send: j1939-msg: {}, arb-id: {:08x}".format(msg, msg.arbitration_id.can_id))
            can_message = Message(arbitration_id=msg.arbitration_id.can_id,
                                  is_extended_id=True,
                                  dlc=len(msg.data),
                                  data=msg.data)

            logging.debug("j1939.send: calling can_bus_send: can-msg: {}".format(can_message))
            try:
                self.can_bus.send(can_message)
            except CanError:
                if self._ignore_can_send_error:
                    pass
                raise

    def shutdown(self):
        self.can_notifier._running = False
        self.can_bus.shutdown()
        #self.j1939_notifier.running.clear()
        super(Bus, self).shutdown()

    def _send_key_response(self, pdu):
        logging.debug("PI04: _send_key_response src=%d, pdu=%s" % (pdu.source, pdu))
        src = pdu.destination
        dest = pdu.source
        logging.debug("PI05: new PDU, src=%d, dest=%d" % (src, dest))
        pdu.destination = dest
        logging.debug("PI05: new PDU.dest = %d" % (pdu.destination))
        pdu.source = src
        logging.debug("PI06: newPDU = %s" % (pdu))

        logging.debug("PI04: _send_key_response src/dest flipped pdu=%s" % (pdu))
        assert(pdu.data[0] == 4) # only support long key for now

        data = pdu.data
        assert(len(data) == 8)

        # seed = (data[5] << 24) + (data[4] << 16) + (data[3] << 8) + data[2]
        # if self._key_generation_fcn is None:
        #     return None
        # key = self._key_generation_fcn(seed)

        seed = (data[5] << 24) + (data[4] << 16) + (data[3] << 8) + data[2]
        #print(f"j1939::seed:{seed}")
        if self._key_generation_fcn is None:
            #print(f"Failed keygen")
            return None
        key = self._key_generation_fcn(seed)
        #print(f"j1939::seed:0x{seed:8x}")
        #print(f"j1939::key:0x{key:8x}")



        logging.debug("PI03: _send_key_response Seed: 0x%08x yields key: 0x%08x" % (seed, key))

        data[5] = (key >> 24) & 0xff
        data[4] = (key >> 16) & 0xff
        data[3] = (key >>  8) & 0xff
        data[2] = (key) & 0xff
        data[1] = 1

        pdu.data = data

        #print(f"j1939::key:pdu:data:0x{key:8x}")

        self.send(pdu)

        return None

    def _process_incoming_message(self, msg):
        logging.debug("PI01: Processing incoming message: instance={}, msg=  {}".format(self, msg))
        arbitration_id = ArbitrationID()
        arbitration_id.can_id = msg.arbitration_id
        if arbitration_id.pgn.is_destination_specific:
            arbitration_id.pgn.value -= arbitration_id.pgn.pdu_specific

        pdu = self._pdu_type(timestamp=msg.timestamp, data=msg.data, info_strings=[])
        pdu.arbitration_id.can_id = msg.arbitration_id
        pdu.info_strings = []
        pdu.radix = 16

        logging.debug("PI02a: arbitration_id.pgn.value = 0x{:04x} ({})".format(arbitration_id.pgn.value, arbitration_id.pgn.value))
        logging.debug("PI02b: PGN_TP_SEED_REQUEST = {}".format(PGN_TP_SEED_REQUEST)) 

        logging.debug("PI02c: self._key_generation_fcn = {}".format(self._key_generation_fcn)) 

        if arbitration_id.pgn.value == PGN_TP_CONNECTION_MANAGEMENT:
            logging.debug("PGN_TP_CONNECTION_MANAGEMENT")
            retval = self._connection_management_handler(pdu)
        elif arbitration_id.pgn.value == PGN_TP_DATA_TRANSFER:
            logging.debug("PGN_TP_DATA_TRANSFER")
            retval = self._data_transfer_handler(pdu)
        elif (arbitration_id.pgn.value == PGN_TP_SEED_REQUEST) and (self._key_generation_fcn is not None):
            logging.debug("PGN_TP_SEED_REQUEST")
            retval = self._send_key_response(pdu)
        else:
            logging.debug("PGN_PDU generic")
            retval = pdu

        logging.debug("_process_incoming_message: returning %s" % (retval))
        return retval

    def _connection_management_handler(self, msg):
        logging.debug("MP00: _connection_management_handler: %s, cmd=%s" % (msg, msg.data[0]))
        if len(msg.data) == 0:
            msg.info_strings.append("Invalid connection management message - no data bytes")
            return msg
        cmd = msg.data[0]
        retval = None

        if cmd == CM_MSG_TYPE_RTS:
            retval = self._process_rts(msg)
        elif cmd == CM_MSG_TYPE_CTS:
            retval = self._process_cts(msg)
        elif cmd == CM_MSG_TYPE_EOM_ACK:
            retval = self._process_eom_ack(msg)
        elif cmd == CM_MSG_TYPE_BAM:
            retval = self._process_bam(msg)
        elif cmd == CM_MSG_TYPE_ABORT:
            retval = self._process_abort(msg)

        logging.debug("_connection_management_handler: returning %s" % (retval))
        return retval

    def _data_transfer_handler(self, msg):
        logging.debug("_data_transfer_handler:")
        msg_source = msg.arbitration_id.source_address
        pdu_specific = msg.arbitration_id.pgn.pdu_specific

        if msg_source in self._incomplete_received_pdus:

            logging.debug("in self._incomplete_received_pdus:")
            if pdu_specific in self._incomplete_received_pdus[msg_source]:
                logging.debug("in self._incomplete_received_pdus[msg_source]:")
                self._incomplete_received_pdus[msg_source][pdu_specific].data.extend(msg.data[1:])
                total = self._incomplete_received_pdu_lengths[msg_source][pdu_specific]["total"]
                if len(self._incomplete_received_pdus[msg_source][pdu_specific].data) >= total:
                    logging.debug("allReceived: %s" % type(self._incomplete_received_pdus[msg_source]))
                    logging.debug("allReceived: %s" % pprint.pformat(self._incomplete_received_pdus[msg_source]))
                    logging.debug("allReceived: %s from 0x%x" % (type(self._incomplete_received_pdus[msg_source][pdu_specific]), pdu_specific))
                    logging.debug("allReceived: %s" % (self._incomplete_received_pdus[msg_source][pdu_specific]))
                    if pdu_specific == DESTINATION_ADDRESS_GLOBAL:
                        logging.debug("pdu_specific == DESTINATION_ADDRESS_GLOBAL")
                        # Looks strange but makes sense - in the absence of explicit flow control,
                        # the last CAN packet in a long message *is* the end of message acknowledgement
                        return self._process_eom_ack(msg)

                    # Find a Node object so we can search its list of known node addresses for this node
                    # so we can find if we are responsible for sending the EOM ACK message
                    # TODO: Was self.j1939_notifier.listeners

                    send_ack = any(True for (_listener, l_notifier) in self.node_queue_list
                            if isinstance(_listener, Node) and
                                (_listener.address == pdu_specific or pdu_specific in _listener.address_list))

                    #send_ack = any(True for l in self.can_notifier.listeners
                    #               if isinstance(l, Node) and (l.address == pdu_specific or
                    #                                           pdu_specific in l.address_list))
                    if send_ack:
                        arbitration_id = ArbitrationID()
                        arbitration_id.pgn.value = PGN_TP_CONNECTION_MANAGEMENT
                        arbitration_id.pgn.pdu_specific = msg_source
                        arbitration_id.source_address = pdu_specific
                        arbitration_id.destination_address = 0x17

                        total_length = self._incomplete_received_pdu_lengths[msg_source][pdu_specific]["total"]
                        _num_packages = self._incomplete_received_pdu_lengths[msg_source][pdu_specific]["num_packages"]
                        pgn = self._incomplete_received_pdus[msg_source][pdu_specific].arbitration_id.pgn
                        pgn_msb = ((pgn.value & 0xFF0000) >> 16)
                        _pgn_middle = ((pgn.value & 0x00FF00) >> 8)
                        _pgn_lsb = 0

                        logging.debug("in send_ack: arbitration_id=[%s], can_id=[%x], destAdder=0x%0x" %
                                (arbitration_id, int(arbitration_id.can_id), arbitration_id.destination_address))
                        logging.debug("in send_ack: " %
                                ())
                        div, mod = divmod(total_length, 256)
                        can_message = Message(arbitration_id=arbitration_id.can_id,
                                              is_extended_id=True,
                                              dlc=8,
                                              data=[CM_MSG_TYPE_EOM_ACK,
                                                    mod,  # total_length % 256,
                                                    div,  # total_length / 256,
                                                    _num_packages,
                                                    0xFF,
                                                    _pgn_lsb,
                                                    _pgn_middle,
                                                    pgn_msb])
                        try:
                            self.can_bus.send(can_message)
                        except CanError:
                            if self._ignore_can_send_error:
                                pass
                            raise

                    logging.debug("_data_transfer_handler: returning %s" % (msg))
                    return self._process_eom_ack(msg)

    def _process_rts(self, msg):
        logging.debug("process_rts, source=0x%x" % (msg.arbitration_id.source_address))
        if msg.arbitration_id.source_address not in self._incomplete_received_pdus:
            self._incomplete_received_pdus[msg.arbitration_id.source_address] = {}
            self._incomplete_received_pdu_lengths[msg.arbitration_id.source_address] = {}

        # Delete any previous messages that were not finished correctly
        if msg.arbitration_id.pgn.pdu_specific in self._incomplete_received_pdus[msg.arbitration_id.source_address]:
            del self._incomplete_received_pdus[msg.arbitration_id.source_address][msg.arbitration_id.pgn.pdu_specific]
            del self._incomplete_received_pdu_lengths[msg.arbitration_id.source_address][
                msg.arbitration_id.pgn.pdu_specific]

        if msg.data[0] == CM_MSG_TYPE_BAM:
            logging.debug("CM_MSG_TYPE_BAM")
            self._incomplete_received_pdus[msg.arbitration_id.source_address][0xFF] = self._pdu_type()
            self._incomplete_received_pdus[msg.arbitration_id.source_address][0xFF].arbitration_id.pgn.value = int(
                ("%.2X%.2X%.2X" % (msg.data[7], msg.data[6], msg.data[5])), 16)
            if self._incomplete_received_pdus[msg.arbitration_id.source_address][
                    0xFF].arbitration_id.pgn.is_destination_specific:
                self._incomplete_received_pdus[msg.arbitration_id.source_address][
                    0xFF].arbitration_id.pgn.pdu_specific = msg.arbitration_id.pgn.pdu_specific
            self._incomplete_received_pdus[msg.arbitration_id.source_address][
                0xFF].arbitration_id.source_address = msg.arbitration_id.source_address
            self._incomplete_received_pdus[msg.arbitration_id.source_address][0xFF].data = []
            _message_size = int("%.2X%.2X" % (msg.data[2], msg.data[1]), 16)
            self._incomplete_received_pdu_lengths[msg.arbitration_id.source_address][0xFF] = {"total": _message_size,
                                                                                              "chunk": 255,
                                                                                              "num_packages": msg.data[
                                                                                                  3], }
        else:
            logging.debug("not CM_MSG_TYPE_BAM, source=0x%x" % (msg.arbitration_id.source_address))
            self._incomplete_received_pdus[msg.arbitration_id.source_address][
                msg.arbitration_id.pgn.pdu_specific] = self._pdu_type()
            self._incomplete_received_pdus[msg.arbitration_id.source_address][
                msg.arbitration_id.pgn.pdu_specific].arbitration_id.pgn.value = int(
                ("%.2X%.2X%.2X" % (msg.data[7], msg.data[6], msg.data[5])), 16)
            if self._incomplete_received_pdus[msg.arbitration_id.source_address][
                    msg.arbitration_id.pgn.pdu_specific].arbitration_id.pgn.is_destination_specific:
                self._incomplete_received_pdus[msg.arbitration_id.source_address][
                    msg.arbitration_id.pgn.pdu_specific].arbitration_id.pgn.pdu_specific = msg.arbitration_id.pgn.pdu_specific
            self._incomplete_received_pdus[msg.arbitration_id.source_address][
                msg.arbitration_id.pgn.pdu_specific].arbitration_id.source_address = msg.arbitration_id.source_address
            self._incomplete_received_pdus[msg.arbitration_id.source_address][
                msg.arbitration_id.pgn.pdu_specific].data = []
        _message_size = int("%.2X%.2X" % (msg.data[2], msg.data[1]), 16)
        self._incomplete_received_pdu_lengths[msg.arbitration_id.source_address][
            msg.arbitration_id.pgn.pdu_specific] = {"total": _message_size, "chunk": 255, "num_packages": msg.data[3], }

        if msg.data[0] != CM_MSG_TYPE_BAM:
            logging.debug("not CM_MSG_TYPE_BAM--2")
            logging.debug("self.can_notifier.listeners = %s" % self.can_notifier.listeners)
            logging.debug("self.node_queue_list = %s" % self.node_queue_list)

            #for _listener in self.can_notifier.listeners:
            for (_listener, l_notifier) in self.node_queue_list:
                logging.debug("MIL2: _listner/l_notifier = %s/%s" % (_listener, l_notifier))
                if isinstance(_listener, Node):
                    logging.debug("6, dest=0x%x" % (msg.arbitration_id.source_address))
                    # find a Node object so we can search its list of known node addresses
                    # for this node - if we find it we are responsible for sending the CTS message
                    logging.debug("MIL3: Node: %s" % (Node))
                    logging.debug("MIL3: _listener.address: %s" % (_listener.address))
                    logging.debug("MIL3: msg.arbitration_id.pgn.pdu_specific: %s" % (msg.arbitration_id.pgn.pdu_specific))
                    logging.debug("MIL3: _listener.address_list: %s" % (_listener.address_list))
                    if _listener.address == msg.arbitration_id.pgn.pdu_specific or \
                            msg.arbitration_id.pgn.pdu_specific in _listener.address_list:
                        _cts_arbitration_id = ArbitrationID(source_address=msg.arbitration_id.pgn.pdu_specific)
                        _cts_arbitration_id.pgn.value = PGN_TP_CONNECTION_MANAGEMENT
                        _cts_arbitration_id.pgn.pdu_specific = msg.arbitration_id.source_address
                        _cts_arbitration_id.destination_address = msg.arbitration_id.source_address
                        _data = [0x11, msg.data[4], 0x01, 0xFF, 0xFF]
                        _data.extend(msg.data[5:])
                        logging.debug("send CTS: AID: %s" % _cts_arbitration_id)
                        cts_msg = Message(is_extended_id=True, arbitration_id=_cts_arbitration_id.can_id, data=_data,
                                          dlc=8)

                        # send clear to send
                        logging.debug("send CTS: %s" % cts_msg)
                        try:
                            self.can_bus.send(cts_msg)
                        except CanError:
                            if self._ignore_can_send_error:
                                pass
                            raise
                        return

    """
                    #
                    # MIL: This is the wrong way around this, I should have a node assigned.
                    #
                    elif _listener is None and l_notifier is not None:
                        logging.debug("7, dest=0x%x" % (msg.arbitration_id.source_address))
                        # find a Node object so we can search its list of known node addresses
                        # for this node - if we find it we are responsible for sending the CTS message
                        if msg.arbitration_id.pgn.pdu_specific :
                            _cts_arbitration_id = ArbitrationID(source_address=msg.arbitration_id.pgn.pdu_specific)
                            _cts_arbitration_id.pgn.value = PGN_TP_CONNECTION_MANAGEMENT
                            _cts_arbitration_id.pgn.pdu_specific = msg.arbitration_id.source_address
                            _cts_arbitration_id.destination_address = msg.arbitration_id.source_address
                            _data = [0x11, msg.data[4], 0x01, 0xFF, 0xFF]
                            _data.extend(msg.data[5:])
                            logging.debug("send CTS: AID: %s" % _cts_arbitration_id)
                            cts_msg = Message(is_extended_id=True, arbitration_id=_cts_arbitration_id.can_id, data=_data,
                                              dlc=8)

                            # send clear to send
                            logging.debug("send CTS: %s" % cts_msg)
                            self.can_bus.send(cts_msg)
                            return
    """

    def _process_cts(self, msg):
        logging.debug("_process_cts")
        logging.debug("MIL8: cts message is: %s" % msg)
        #logging.debug("MIL8:    len(pdu-send-buffer) = %d" % len(self._incomplete_transmitted_pdus[0][23]))


        if msg.arbitration_id.pgn.pdu_specific in self._incomplete_transmitted_pdus:
            if msg.arbitration_id.source_address in self._incomplete_transmitted_pdus[
                    msg.arbitration_id.pgn.pdu_specific]:
                # Next packet number in CTS message (Packet numbers start at 1 not 0)
                start_index = msg.data[2] - 1
                # Using total number of packets in CTS message
                end_index = start_index + msg.data[1]
                for _msg in self._incomplete_transmitted_pdus[msg.arbitration_id.pgn.pdu_specific][
                        msg.arbitration_id.source_address][start_index:end_index]:
                    logging.debug("MIL8:        msg=%s" % (_msg))
                    # TODO: Needs to be pacing if we get this working...
                    try:
                        # Shouldent send a J1939 PDU as a CAN Message unless we are careful
                        canMessage =  Message(arbitration_id=_msg.arbitration_id, data=_msg.data)
                        self.can_bus.send(canMessage)
                    except CanError:
                        
                        if self._ignore_can_send_error:
                            pass
                        raise
        logging.debug("MIL8:    _process_cts complete")

    def _process_eom_ack(self, msg):
        logging.debug("_process_eom_ack")
        if (msg.arbitration_id.pgn.value - msg.arbitration_id.pgn.pdu_specific) == PGN_TP_DATA_TRANSFER:
            logging.debug("_process_eom_ack: PGN_TP_DATA_TRANSFER")
            self._incomplete_received_pdus[msg.arbitration_id.source_address][
                msg.arbitration_id.pgn.pdu_specific].timestamp = msg.timestamp
            retval = copy.deepcopy(
                self._incomplete_received_pdus[msg.arbitration_id.source_address][msg.arbitration_id.pgn.pdu_specific])
            retval.data = retval.data[:self._incomplete_received_pdu_lengths[msg.arbitration_id.source_address][
                msg.arbitration_id.pgn.pdu_specific]["total"]]
            del self._incomplete_received_pdus[msg.arbitration_id.source_address][msg.arbitration_id.pgn.pdu_specific]
            del self._incomplete_received_pdu_lengths[msg.arbitration_id.source_address][
                msg.arbitration_id.pgn.pdu_specific]
        else:
            logging.debug("_process_eom_ack: not PGN_TP_DATA_TRANSFER")
            if msg.arbitration_id.pgn.pdu_specific in self._incomplete_received_pdus:
                if msg.arbitration_id.source_address in self._incomplete_received_pdus[
                        msg.arbitration_id.pgn.pdu_specific]:
                    self._incomplete_received_pdus[msg.arbitration_id.pgn.pdu_specific][
                        msg.arbitration_id.source_address].timestamp = msg.timestamp
                    retval = copy.deepcopy(self._incomplete_received_pdus[msg.arbitration_id.pgn.pdu_specific][
                        msg.arbitration_id.source_address])
                    retval.data = retval.data[:
                                              self._incomplete_received_pdu_lengths[msg.arbitration_id.pgn.pdu_specific][
                                                  msg.arbitration_id.source_address]["total"]]
                    del self._incomplete_received_pdus[msg.arbitration_id.pgn.pdu_specific][
                        msg.arbitration_id.source_address]
                    del self._incomplete_received_pdu_lengths[msg.arbitration_id.pgn.pdu_specific][
                        msg.arbitration_id.source_address]
                else:
                    retval = None
            else:
                retval = None
            if msg.arbitration_id.pgn.pdu_specific in self._incomplete_transmitted_pdus:
                if msg.arbitration_id.source_address in self._incomplete_transmitted_pdus[
                        msg.arbitration_id.pgn.pdu_specific]:
                    del self._incomplete_transmitted_pdus[msg.arbitration_id.pgn.pdu_specific][
                        msg.arbitration_id.source_address]

        logging.debug("_process_eom_ack: returning %s" % (retval))
        return retval

    def _process_bam(self, msg):
        self._process_rts(msg)

    def _process_abort(self, msg):
        if msg.arbitration_id.pgn.pdu_specific in self._incomplete_received_pdus:
            if msg.source in self._incomplete_received_pdus[msg.arbitration_id.pgn.pdu_specific]:
                del self._incomplete_received_pdus[msg.arbitration_id.pgn.pdu_specific][
                    msg.arbitration_id.source_address]

    def _throttler_function(self):
        while self.can_notifier._running:
            _msg = None
            try:
                _msg = self._long_message_segment_queue.get(timeout=0.1)
            except Empty:
                pass
            if _msg is not None:
                try:
                    self.can_bus.send(_msg)
                    time.sleep(0.05)
                except CanError:
                    if self._ignore_can_send_error:
                        pass
                    raise

    @property
    def transmissions_in_progress(self):
        retval = 0
        for _tx_address in self._incomplete_transmitted_pdus:
            retval += len(self._incomplete_transmitted_pdus[_tx_address])
        for _rx_address in self._incomplete_received_pdus:
            retval += len(self._incomplete_received_pdus[_rx_address])
        return retval

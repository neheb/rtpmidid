#!env/bin/python3
import alsaseq
import sys
import time
import socket
import struct
import random
import select
import zeroconf
import queue
import traceback
import os
import logging

logging.basicConfig(format='%(levelname)-8s | %(message)s', level=logging.DEBUG)

logger = logging.getLogger(__name__)
SSRC = random.randint(0, 0xffffffff)
NAME = '%s - ALSA SEQ' % (socket.gethostname())
PORT = 10008
DEBUG = True

task_queue = queue.Queue()
task_ready_fds = os.pipe2(os.O_NONBLOCK)
task_ready_fds = [os.fdopen(task_ready_fds[0], 'rb'), os.fdopen(task_ready_fds[1], 'wb')]
rtp_midi = None


def main():
    global rtp_midi
    logger.info("RTP/MIDI v.0.1")
    event_dispatcher = EventDispatcher()
    rtp_midi = RTPMidi()

    # zero conf setup
    zeroconfp = zeroconf.Zeroconf()
    apple_midi_listener = AppleMidiListener()
    zeroconf.ServiceBrowser(zeroconfp, "_apple-midi._udp.local.", apple_midi_listener)

    alsaseq.client("Network", 1, 1, False)

    for arg in sys.argv[1:]:
        hostport = arg.split(':')
        rtp_midi.connect_to(*hostport)

    event_dispatcher.add(alsaseq.fd(), process_alsa)
    event_dispatcher.add(rtp_midi.filenos(), rtp_midi.data_ready)
    event_dispatcher.add(task_ready_fds[0].fileno(), process_tasks, task_ready_fds[0])

    logger.debug("Loop")
    n = 0
    while True:
        print("Event count: %s" % n, end="\r")
        event_dispatcher.wait_and_dispatch_one()
        n += 1


class EventDispatcher:
    def __init__(self):
        self.epoll = select.epoll()
        self.fdmap = {}

    def add(self, fdlike, func, *args, **kwargs):
        for fd in maybe_wrap(fdlike):
            self.fdmap[fd] = (func, args, kwargs)
            self.epoll.register(fd, select.EPOLLIN | select.EPOLLHUP | select.EPOLLERR)

    def wait_and_dispatch_one(self):
        for (fd, event) in self.epoll.poll():
            # logger.debug("Data ready fd: %d. avail: %s", fd, self.fdmap)
            f_tuple = self.fdmap.get(fd)
            if not f_tuple:
                logger.error("Got data for an unmanaged fd")
                continue

            f, args, kwargs = f_tuple
            if not args and not kwargs:
                args = (fd,)
            try:
                f(*args, **kwargs)
            except Exception:
                logger.error("Error executing: %s", f.__name__)
                traceback.print_exc()


def process_alsa(fd):
    alsaseq.inputpending()
    ev = alsaseq.input()
    if DEBUG:
        logger.debug("ALSA: %s", ev_to_dict(ev))
        midi = ev_to_midi(ev)
        if midi:
            logger.debug("ALSA to MIDI: %s", [hex(x) for x in midi])
            rtp_midi.send(midi)


def add_task(fn, **kwargs):
    task_queue.put((fn, kwargs))
    task_ready_fds[1].write(b"1")
    task_ready_fds[1].flush()
    logger.debug("Add task to queue")


def process_tasks(fd):
    fd.read(1024)  # clear queue
    while not task_queue.empty():
        (fn, kwargs) = task_queue.get()
        try:
            fn(**kwargs)
        except Exception as e:
            logger.error("Error executing task: %s", e)
            traceback.print_exc()


class AppleMidiListener:
    def remove_service(self, zeroconf, type, name):
        info = zeroconf.get_service_info(type, name)
        logger.info("Service removed: %s, %s, %s", repr(info.get_name()), [x for x in info.address], info.port)

    def add_service(self, zeroconf, type, name):
        info = zeroconf.get_service_info(type, name)
        logger.info("Service added: %s, %s, %s", repr(info.get_name()), [x for x in info.address], info.port)
        add_task(add_applemidi, address=info.address, port=info.port)


def add_applemidi(address, port):
    rtp_midi.connect_to('.'.join(str(x) for x in address), port)


class RTPMidi:
    def __init__(self, local_port=PORT):
        """
        Opens an RTP connection to that port.
        """
        self.peers = {}
        self.control_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.control_sock.bind(('0.0.0.0', local_port))
        self.midi_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.midi_sock.bind(('0.0.0.0', local_port + 1))

    def filenos(self):
        return (self.control_sock.fileno(), self.midi_sock.fileno())

    def data_ready(self, fd):
        # logger.debug(
        #     "Data ready fd: %d, midi: %d, control: %d",
        #     fd, self.midi_sock.fileno(), self.control_sock.fileno()
        # )
        if fd == self.midi_sock.fileno():
            self.process_midi()
        elif fd == self.control_sock.fileno():
            self.process_control()
        else:
            logger.error(
                "Dont know who to send data fd: %d, midi: %d, control: %d",
                fd, self.midi_sock.fileno(), self.control_sock.fileno()
            )

    def connect_to(self, hostname, port):
        port = int(port)
        id = random.randint(0, 0xFFFFFFFF)
        peer = RTPConnection(self, hostname, port, id=id, is_control=True)
        # midi = RTPConnection(self, self.midi_sock, hostname, port+1, id=id)
        self.peers[id] = peer

    def initiator_is_peer(self, initiator, peer):
        """
        We know control and midi from an internal random id, but receive some
        messages from the remote SSRC. Do the mapping.

        Both at the same mapping, expecting no collissions.
        """
        logger.debug("Old initiator: %X is now peer: %X", initiator, peer)

        self.peers[peer] = self.peers[initiator]
        del self.peers[initiator]

    def process_midi(self):
        (source, msg) = self.remote_data_read(self.midi_sock)
        for ev in midi_to_evs(msg):
            peer = self.peers.get(source)
            if peer and ev:
                    logger.debug("Network MIDI from: %s event: %s", peer.name, ev_to_dict(ev))
                    alsaseq.output(ev)
            elif not peer:
                logger.warn("Unknown source, ignoring. TODO: Send disconnect.")

    def process_control(self):
        (source, msg) = self.remote_data_read(self.control_sock)
        logger.debug("Got control from %s: %s", source and hex(source), to_hex_str(msg))

    def rtp_decode(self, msg):
        (flags, type, sequence_nr, timestamp, source) = struct.unpack("!BBHLL", msg)

        return (flags, type, sequence_nr, timestamp, source)

    def remote_data_read(self, sock):
        (msg, from_) = sock.recvfrom(1500)

        is_command = struct.unpack("!H", msg[:2])[0] == 0xFFFF
        # logger.debug("Got data at sock: %s, is_command: %s", sock, is_command)
        if is_command:
            command = struct.unpack("!H", msg[2:4])[0]
            if command == RTPConnection.Commands.OK:
                initiator = struct.unpack("!L", msg[8:12])[0]
                peer = self.peers.get(initiator)
                if peer:
                    return peer.accepted_connection(msg)
                else:
                    logger.error("Unknown initiator: %X", initiator)
            elif command == RTPConnection.Commands.CK:
                initiator = struct.unpack("!L", msg[4:8])[0]
                peer = self.peers.get(initiator)
                if peer:
                    return peer.recv_sync(msg)
                else:
                    logger.error("Unknown initiator: %X", initiator)
            else:
                logger.error(
                    "Unimplemented command %X. Maybe RTP message (reuse of connection). Maybe MIDI command.",
                    command
                )
            return (None, b'')

        rtp = self.rtp_decode(msg[:12])
        source = rtp[4]
        return (source, msg[13:])

    def send(self, msg):
        for peer in self.peers.values():
            peer.send_midi(msg)


class RTPConnection:
    class State:
        NOT_CONNECTED = 0
        SENT_REQUEST = 1
        CONNECTED = 2
        SYNC = 3

    class Commands:
        IN = 0x494e  # Just the chars
        OK = 0x4f4b
        NO = 0x4e4f
        BY = 0x4259
        CK = 0x434b

    def __init__(self, rtpmidi, remote_host, remote_port, id, is_control=False):
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.state = RTPConnection.State.NOT_CONNECTED
        self.name = None
        self.id = id or random.randint(0, 0xffffffff)
        self.rtpmidi = rtpmidi
        self.is_control = is_control
        self.conn_start = None
        self.seq1 = random.randint(0, 0xFFFF)
        self.seq2 = random.randint(0, 0xFFFF)
        self.connect(rtpmidi.control_sock, self.remote_port)
        self.connect(rtpmidi.midi_sock, self.remote_port+1)

    def connect(self, sock, port):
        signature = 0xFFFF
        command = RTPConnection.Commands.IN
        protocol = 2
        sender = SSRC

        logger.info("[%X] Connect to %s:%d", self.id, self.remote_host, port)
        msg = struct.pack("!HHLLL", signature, command, protocol, self.id, sender) + NAME.encode('utf8') + b'\0'
        sock.sendto(msg, (self.remote_host, port))
        self.state = RTPConnection.State.SENT_REQUEST

    def sync(self):
        self.state = RTPConnection.State.SYNC
        logger.debug("[%X] Sync", self.id)

        t1 = int(time.time() * 10000)  # current time or something in 100us units
        msg = struct.pack(
            "!HHLbbHQQQ",
            0xFFFF, RTPConnection.Commands.CK, self.id, 0, 0, 0,
            t1, 0, 0
        )
        self.rtpmidi.control_sock.sendto(msg, (self.remote_host, self.remote_port))

    def recv_sync(self, msg):
        print("Sync: ", to_hex_str(msg))
        (sender, count, _, _, t1, t2, t3) = struct.unpack("!LbbHQQQ", msg[4:])
        print(sender, count, t1, t2, t3)
        if count == 0:
            self.sync1(sender, t1)
        if count == 1:
            self.sync2(sender, t1, t2)
        if count == 2:
            self.sync3(sender, t1, t2, t3)
        return (None, b'')

    def sync1(self, sender, t1):
        logger.debug("[%X] Sync1", self.id)
        t2 = int(self.time() * 10000)  # current time or something in 100us units
        msg = struct.pack(
            "!HHLbbHQQQ",
            0xFFFF, RTPConnection.Commands.CK, self.id, 1, 0, 0,
            t1, t2, 0
        )
        self.rtpmidi.control_sock.sendto(msg, (self.remote_host, self.remote_port))

    def sync2(self, sender, t1, t2):
        logger.debug("[%X] Sync2", self.id)
        t3 = int(self.time() * 10000)  # current time or something in 100us units
        self.offset = ((t1 + t3) / 2) - t2
        logger.info("[%X] Offset is now: %d for: %d ", self.id, self.offset, sender)
        msg = struct.pack(
            "!HHLbbHQQQ",
            0xFFFF, RTPConnection.Commands.CK, self.id, 2, 0, 0,
            t1, t2, t3
        )
        self.rtpmidi.control_sock.sendto(msg, (self.remote_host, self.remote_port))

    def sync3(self, sender, t1, t2, t3):
        logger.debug("[%X] Sync3", self.id)
        self.offset = ((t1 + t3) / 2) - t2
        logger.info("[%X] Offset is now: %d for: %d ", self.id, self.offset, sender)

    def accepted_connection(self, msg):
        (protocol, rtp_id, sender) = struct.unpack("!LLL", msg[4:16])
        name = ""
        for m in msg[16:]:
            if m == b'\0':
                break
            name += chr(m)
        assert self.id == rtp_id, "Got wrong message: " + to_hex_str(msg)
        logger.info(
            "[%X] Connected local_port host: %s:%d, name: %s, remote_id: %X",
            self.id, self.remote_host, self.remote_port, repr(name), sender
        )
        self.conn_start = time.time()
        self.name = name
        self.state = RTPConnection.State.CONNECTED
        if self.is_control:
            self.sync()
        self.rtpmidi.initiator_is_peer(self.id, sender)
        self.id = sender
        return (None, [])

    def time(self):
        return time.time() - self.conn_start

    def send_midi(self, msg):
        if not self.conn_start:
            # Not yet connected
            return
        if len(msg) > 16:
            raise Exception("Current implementation max event size is 16 bytes")
        # Short header, no fourname, no deltatime, status in first byte, 4 * length. So just length.
        rtpmidi_header = [len(msg)]
        timestamp = int(self.time() * 1000)
        self.seq1 += 1
        self.seq2 += 1

        rtpheader = [
            0x80, 0x61,
            byten(self.seq1, 1), byten(self.seq1, 0),
            # byten(self.seq2, 1), byten(self.seq2, 0),  # sequence nr
            byten(timestamp, 3), byten(timestamp, 2), byten(timestamp, 1), byten(timestamp, 0),
            byten(self.id, 3), byten(self.id, 2), byten(self.id, 1), byten(self.id, 0),  # sequence nr
        ]
        msg = bytes(rtpheader) + bytes(rtpmidi_header) + bytes(msg)

        self.rtpmidi.midi_sock.sendto(msg, (self.remote_host, self.remote_port + 1))

        print(self.name, ' '.join(["%2X" % x for x in msg]))

    def __str__(self):
        return "[%X] %s" % (self.id, self.name)


def to_hex_str(msg):
    return " ".join(hex(x) for x in msg)


def byten(nr, n):
    return (nr >> (8*n)) & 0x0FF


def ev_to_dict(ev):
    """
    Converts an event to a struct. Mainly for debugging.
    """
    (type, flags, tag, queue, timestamp, source, destination, data) = ev
    return {
        "type": hex(type),
        "flags": flags,
        "tag": tag,
        "queue": queue,
        "timestamp": timestamp,
        "source": source,
        "destination": destination,
        "data": data,
    }


def midi_to_evs(source):
    current = 0
    data = []
    evlen = 2
    for c in source:
        if c & 0x080:
            current = c
            data = [current]
            if current in [0xC0, 0xD0]:
                evlen = 1
            else:
                evlen = 2
        elif current != 0xF0 and len(data) == evlen:
            data.append(c)
            yield midi_to_ev(data)
            data = [current]
        elif current == 0xF0 and c == 0x7F:
            yield midi_to_ev(data)
            data = ""
        else:
            data.append(c)

    if len(data) == 2:
        yield midi_to_ev(data)


# check names at https://github.com/Distrotech/alsa-lib/blob/distrotech-alsa-lib/include/seq_event.h
MIDI_TO_EV = {
    0x80: alsaseq.SND_SEQ_EVENT_NOTEOFF,  # note off
    0x90: alsaseq.SND_SEQ_EVENT_NOTEON,  # note on
    0xA0: alsaseq.SND_SEQ_EVENT_KEYPRESS,  # Poly key pressure / After touch
    0xB0: alsaseq.SND_SEQ_EVENT_CONTROLLER,  # CC
    # 0xC0: alsaseq.SND_SEQ_EVENT_PGMCHANGE,   # Program change / 1b
    # 0xD0: alsaseq.SND_SEQ_EVENT_CHANPRESS,   # Channel key pres / 1b
    0xE0: alsaseq.SND_SEQ_EVENT_PITCHBEND,  # pitch bend
}
EV_TO_MIDI = dict({v: k for k, v in MIDI_TO_EV.items()})  # just the revers


def midi_to_ev(event):
    type = MIDI_TO_EV.get(event[0] & 0x0F0)
    if not type:
        print("Unknown MIDI Event %s" % to_hex_str(event))
        return None

    len_event = len(event)

    if type == alsaseq.SND_SEQ_EVENT_PITCHBEND:
        _, lsb, msb = event
        return (
            type,
            0,
            0,
            253,
            (0, 0),
            (0, 0),
            (0, 0),

            (0, 0, 0, 0, 0, (msb << 7) + lsb)
        )
    elif type in (alsaseq.SND_SEQ_EVENT_NOTEON, alsaseq.SND_SEQ_EVENT_NOTEOFF) and len_event == 3:
        _, param1, param2 = event
        return (
            type,
            0,
            0,
            253,
            (0, 0),
            (0, 0),
            (0, 0),
            (0, param1, param2, 0, 0)
        )
    elif type == alsaseq.SND_SEQ_EVENT_CONTROLLER and len_event == 3:
        _, param1, param2 = event
        return (
            type,
            0,
            0,
            253,
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0, 0, 0, param1, param2)
        )
    logger.warn("Unimplemented MIDI event: %s, type: %s", event[0], type)
    return None


def ev_to_midi(ev):
    if ev[0] in (alsaseq.SND_SEQ_EVENT_NOTEON, alsaseq.SND_SEQ_EVENT_NOTEOFF):
        return (EV_TO_MIDI[ev[0]], ev[7][1], ev[7][2])
    if ev[0] == alsaseq.SND_SEQ_EVENT_CONTROLLER:
        return (EV_TO_MIDI[ev[0]], ev[7][4], ev[7][5])
    if ev[0] == alsaseq.SND_SEQ_EVENT_PITCHBEND:
        n = ev[7][5]
        return (EV_TO_MIDI[ev[0]], (n >> 7) & 0x07F, n & 0x07F)
    if ev[0] == 66:
        logger.info("New connection.")
        return None
    logger.warn("Unimplemented ALSA event: %d" % ev[0])
    return None


def maybe_wrap(maybelist):
    """
    Always returns an iterable. If None an empty one, if one element, a tuple with
    one element. If a list or tuple itself.
    """
    if maybelist is None:
        return ()
    if isinstance(maybelist, (list, tuple)):
        return maybelist
    return (maybelist,)


def test():
    events = list(midi_to_evs([0x90, 10, 10, 0x80, 10, 0]))
    print(events)


if len(sys.argv) == 2 and sys.argv[1] == "test":
    test()
else:
    main()
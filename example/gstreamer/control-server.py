#!/usr/bin/env python3

import os
import sys
import ssl
import logging
import asyncio
import websockets
import argparse
import http

from concurrent.futures._base import TimeoutError

parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
# See: host, port in https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.create_server
parser.add_argument('--addr', default='', help='Address to listen on (default: all interfaces, both ipv4 and ipv6)')
parser.add_argument('--port', default=8443, type=int, help='Port to listen on')
parser.add_argument('--keepalive-timeout', dest='keepalive_timeout', default=600, type=int, help='Timeout for keepalive (in seconds)')
parser.add_argument('--cert-path', default=os.path.dirname(__file__))
parser.add_argument('--disable-ssl', default=False, help='Disable ssl', action='store_true')
parser.add_argument('--health', default='/health', help='Health check route')

options = parser.parse_args(sys.argv[1:])

ADDR_PORT = (options.addr, options.port)
KEEPALIVE_TIMEOUT = options.keepalive_timeout

############### Global data ###############

# Format: {uid: (Peer WebSocketServerProtocol,
#                remote_address,
#                <'session'|room_id|None>)}
peers = dict()
# Format: {caller_uid: callee_uid,
#          callee_uid: caller_uid}
# Bidirectional mapping between the two peers
sessions = dict()
# Format: {room_id: {peer1_id, peer2_id, peer3_id, ...}}
# Room dict with a set of peers in each room
rooms = dict()

############## Server side peer ################


import json
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp

PIPELINE_DESC = '''
webrtcbin name=sendrecv 
 pulsesrc ! audioconvert ! audioresample ! queue ! opusenc ! rtpopuspay !
 queue ! application/x-rtp,media=audio,encoding-name=OPUS,payload=96 ! sendrecv.
'''

class WebRTCClient:
    def __init__(self, our_id, peer_id, server):
        self.our_id = our_id
        self.conn = None
        self.pipe = None
        self.webrtc = None
        self.peer_id = peer_id
        self.server = server

    async def connect(self):
        sslctx = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        self.conn = await websockets.connect(self.server, ssl=sslctx)
        await self.conn.send('HELLO %d' % self.our_id)

    async def setup_call(self):
        await self.conn.send('SESSION {}'.format(self.peer_id))

    def send_sdp_offer(self, offer):
        text = offer.sdp.as_text()
        print ('Sending offer:\n%s' % text)
        msg = json.dumps({'sdp': {'type': 'offer', 'sdp': text}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(msg))

    def on_offer_created(self, promise, _, __):
        promise.wait()
        reply = promise.get_reply()
        offer = reply['offer']
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()
        self.send_sdp_offer(offer)

    def on_negotiation_needed(self, element):
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        element.emit('create-offer', None, promise)

    def send_ice_candidate_message(self, _, mlineindex, candidate):
        icemsg = json.dumps({'ice': {'candidate': candidate, 'sdpMLineIndex': mlineindex}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(icemsg))

    def on_incoming_decodebin_stream(self, _, pad):
        if not pad.has_current_caps():
            print (pad, 'has no caps, ignoring')
            return

        caps = pad.get_current_caps()
        assert (len(caps))
        s = caps[0]
        name = s.get_name()
        if name.startswith('video'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('videoconvert')
            sink = Gst.ElementFactory.make('autovideosink')
            self.pipe.add(q, conv, sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(sink)
        elif name.startswith('audio'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('audioconvert')
            resample = Gst.ElementFactory.make('audioresample')
            sink = Gst.ElementFactory.make('autoaudiosink')
            self.pipe.add(q, conv, resample, sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(resample)
            resample.link(sink)

    def on_incoming_stream(self, _, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return

        decodebin = Gst.ElementFactory.make('decodebin')
        decodebin.connect('pad-added', self.on_incoming_decodebin_stream)
        self.pipe.add(decodebin)
        decodebin.sync_state_with_parent()
        self.webrtc.link(decodebin)

    def start_pipeline(self):
        self.pipe = Gst.parse_launch(PIPELINE_DESC)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        #decrease latency
        rtpbin = self.webrtc.get_by_name("rtpbin")
        rtpbin.set_property("latency", 40)

        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate', self.send_ice_candidate_message)
        self.webrtc.connect('pad-added', self.on_incoming_stream)
        self.pipe.set_state(Gst.State.PLAYING)

    async def handle_sdp(self, message):
        assert (self.webrtc)
        msg = json.loads(message)
        if 'sdp' in msg:
            sdp = msg['sdp']
            assert(sdp['type'] == 'answer')
            sdp = sdp['sdp']
            print ('Received answer:\n%s' % sdp)
            res, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)
            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', answer, promise)
            promise.interrupt()
        elif 'ice' in msg:
            ice = msg['ice']
            candidate = ice['candidate']
            sdpmlineindex = ice['sdpMLineIndex']
            self.webrtc.emit('add-ice-candidate', sdpmlineindex, candidate)

    async def loop(self):
        assert self.conn
        async for message in self.conn:
            if message == 'HELLO':
                await self.setup_call()
            elif message == 'SESSION_OK':
                self.start_pipeline()
            elif message.startswith('ERROR'):
                print (message)
                return 1
            else:
                await self.handle_sdp(message)
        return 0


def check_plugins():
    needed = ["opus", "vpx", "nice", "webrtc", "dtls", "srtp", "rtp",
              "rtpmanager", "videotestsrc", "audiotestsrc"]
    missing = list(filter(lambda p: Gst.Registry.get().find_plugin(p) is None, needed))
    if len(missing):
        print('Missing gstreamer plugins:', missing)
        return False
    return True

async def launch_server_side(peerid, server):
    Gst.init(None)
    our_id = 73  #hardcoded server id, needed: A way to authenticate users and the server, and handle them based on that. Then we need multiple ids for the server, to support multiple connections.
    c = WebRTCClient(our_id,peerid,server)
    await c.connect()
    asyncio.create_task(c.loop())
    
"""
if __name__=='__main__':
    Gst.init(None)
    if not check_plugins():
        sys.exit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument('peerid', help='String ID of the peer to connect to')
    parser.add_argument('--server', help='Signalling server to connect to, eg "wss://127.0.0.1:8443"')
    args = parser.parse_args()
    our_id = 73
    c = WebRTCClient(our_id, args.peerid, args.server)
    asyncio.get_event_loop().run_until_complete(c.connect())
    res = asyncio.get_event_loop().run_until_complete(c.loop())
    sys.exit(res)
"""





############### Helper functions ###############

async def health_check(path, request_headers):
    if path == options.health:
        return http.HTTPStatus.OK, [], b"OK\n"

async def recv_msg_ping(ws, raddr):
    '''
    Wait for a message forever, and send a regular ping to prevent bad routers
    from closing the connection.
    '''
    msg = None
    while msg is None:
        try:
            msg = await asyncio.wait_for(ws.recv(), KEEPALIVE_TIMEOUT)
        except TimeoutError:
            print('Sending keepalive ping to {!r} in recv'.format(raddr))
            await ws.ping()
    return msg

async def disconnect(ws, peer_id):
    '''
    Remove @peer_id from the list of sessions and close our connection to it.
    This informs the peer that the session and all calls have ended, and it
    must reconnect.
    '''
    global sessions
    if peer_id in sessions:
        del sessions[peer_id]
    # Close connection
    if ws and ws.open:
        # Don't care about errors
        asyncio.ensure_future(ws.close(reason='hangup'))

async def cleanup_session(uid):
    if uid in sessions:
        other_id = sessions[uid]
        del sessions[uid]
        print("Cleaned up {} session".format(uid))
        if other_id in sessions:
            del sessions[other_id]
            print("Also cleaned up {} session".format(other_id))
            # If there was a session with this peer, also
            # close the connection to reset its state.
            if other_id in peers:
                print("Closing connection to {}".format(other_id))
                wso, oaddr, _ = peers[other_id]
                del peers[other_id]
                await wso.close()

async def cleanup_room(uid, room_id):
    room_peers = rooms[room_id]
    if uid not in room_peers:
        return
    room_peers.remove(uid)
    for pid in room_peers:
        wsp, paddr, _ = peers[pid]
        msg = 'ROOM_PEER_LEFT {}'.format(uid)
        print('room {}: {} -> {}: {}'.format(room_id, uid, pid, msg))
        await wsp.send(msg)

async def remove_peer(uid):
    await cleanup_session(uid)
    if uid in peers:
        ws, raddr, status = peers[uid]
        if status and status != 'session':
            await cleanup_room(uid, status)
        del peers[uid]
        await ws.close()
        print("Disconnected from peer {!r} at {!r}".format(uid, raddr))


############### Handler functions ###############

async def connection_handler(ws, uid):
    global peers, sessions, rooms
    raddr = ws.remote_address
    peer_status = None
    peers[uid] = [ws, raddr, peer_status]
    print("Registered peer {!r} at {!r}".format(uid, raddr))
    #temporary way to launch server side of webrtc connection, this is the stuff we need to figure out next
    #hardcoded ids: client_id=42; server_id=73
    if uid != 73:
        asyncio.get_event_loop().create_task(launch_server_side(uid, "wss://localhost:8443"))

    while True:
        # Receive command, wait forever if necessary
        msg = await recv_msg_ping(ws, raddr)
        # Update current status
        peer_status = peers[uid][2]
        # We are in a session or a room, messages must be relayed
        if peer_status is not None:
            # We're in a session, route message to connected peer
            if peer_status == 'session':
                other_id = sessions[uid]
                wso, oaddr, status = peers[other_id]
                assert(status == 'session')
                print("{} -> {}: {}".format(uid, other_id, msg))
                await wso.send(msg)
            # We're in a room, accept room-specific commands
            elif peer_status:
                # ROOM_PEER_MSG peer_id MSG
                if msg.startswith('ROOM_PEER_MSG'):
                    _, other_id, msg = msg.split(maxsplit=2)
                    if other_id not in peers:
                        await ws.send('ERROR peer {!r} not found'
                                      ''.format(other_id))
                        continue
                    wso, oaddr, status = peers[other_id]
                    if status != room_id:
                        await ws.send('ERROR peer {!r} is not in the room'
                                      ''.format(other_id))
                        continue
                    msg = 'ROOM_PEER_MSG {} {}'.format(uid, msg)
                    print('room {}: {} -> {}: {}'.format(room_id, uid, other_id, msg))
                    await wso.send(msg)
                elif msg == 'ROOM_PEER_LIST':
                    room_id = peers[peer_id][2]
                    room_peers = ' '.join([pid for pid in rooms[room_id] if pid != peer_id])
                    msg = 'ROOM_PEER_LIST {}'.format(room_peers)
                    print('room {}: -> {}: {}'.format(room_id, uid, msg))
                    await ws.send(msg)
                else:
                    await ws.send('ERROR invalid msg, already in room')
                    continue
            else:
                raise AssertionError('Unknown peer status {!r}'.format(peer_status))
        # Requested a session with a specific peer
        elif msg.startswith('SESSION'):
            print("{!r} command {!r}".format(uid, msg))
            _, callee_id = msg.split(maxsplit=1)
            if callee_id not in peers:
                await ws.send('ERROR peer {!r} not found'.format(callee_id))
                continue
            if peer_status is not None:
                await ws.send('ERROR peer {!r} busy'.format(callee_id))
                continue
            await ws.send('SESSION_OK')
            wsc = peers[callee_id][0]
            print('Session from {!r} ({!r}) to {!r} ({!r})'
                  ''.format(uid, raddr, callee_id, wsc.remote_address))
            # Register session
            peers[uid][2] = peer_status = 'session'
            sessions[uid] = callee_id
            peers[callee_id][2] = 'session'
            sessions[callee_id] = uid
        # Requested joining or creation of a room
        elif msg.startswith('ROOM'):
            print('{!r} command {!r}'.format(uid, msg))
            _, room_id = msg.split(maxsplit=1)
            # Room name cannot be 'session', empty, or contain whitespace
            if room_id == 'session' or room_id.split() != [room_id]:
                await ws.send('ERROR invalid room id {!r}'.format(room_id))
                continue
            if room_id in rooms:
                if uid in rooms[room_id]:
                    raise AssertionError('How did we accept a ROOM command '
                                         'despite already being in a room?')
            else:
                # Create room if required
                rooms[room_id] = set()
            room_peers = ' '.join([pid for pid in rooms[room_id]])
            await ws.send('ROOM_OK {}'.format(room_peers))
            # Enter room
            peers[uid][2] = peer_status = room_id
            rooms[room_id].add(uid)
            for pid in rooms[room_id]:
                if pid == uid:
                    continue
                wsp, paddr, _ = peers[pid]
                msg = 'ROOM_PEER_JOINED {}'.format(uid)
                print('room {}: {} -> {}: {}'.format(room_id, uid, pid, msg))
                await wsp.send(msg)
        else:
            print('Ignoring unknown message {!r} from {!r}'.format(msg, uid))

async def hello_peer(ws):
    '''
    Exchange hello, register peer
    '''
    raddr = ws.remote_address
    hello = await ws.recv()
    hello, uid = hello.split(maxsplit=1)
    if hello != 'HELLO':
        await ws.close(code=1002, reason='invalid protocol')
        raise Exception("Invalid hello from {!r}".format(raddr))
    if not uid or uid in peers or uid.split() != [uid]: # no whitespace
        await ws.close(code=1002, reason='invalid peer uid')
        raise Exception("Invalid uid {!r} from {!r}".format(uid, raddr))
    # Send back a HELLO
    await ws.send('HELLO')
    return uid

async def handler(ws, path):
    '''
    All incoming messages are handled here. @path is unused.
    '''
    raddr = ws.remote_address
    print("Connected to {!r}".format(raddr))
    peer_id = await hello_peer(ws)
    try:
        await connection_handler(ws, peer_id)
    except websockets.ConnectionClosed:
        print("Connection to peer {!r} closed, exiting handler".format(raddr))
    finally:
        await remove_peer(peer_id)

sslctx = None
if not options.disable_ssl:
    # Create an SSL context to be used by the websocket server
    certpath = options.cert_path
    print('Using TLS with keys in {!r}'.format(certpath))
    if 'letsencrypt' in certpath:
        chain_pem = os.path.join(certpath, 'fullchain.pem')
        key_pem = os.path.join(certpath, 'privkey.pem')
    else:
        chain_pem = os.path.join(certpath, 'cert.pem')
        key_pem = os.path.join(certpath, 'key.pem')

    sslctx = ssl.create_default_context()
    try:
        sslctx.load_cert_chain(chain_pem, keyfile=key_pem)
    except FileNotFoundError:
        print("Certificates not found, did you run generate_cert.sh?")
        sys.exit(1)
    # FIXME
    sslctx.check_hostname = False
    sslctx.verify_mode = ssl.CERT_NONE

print("Listening on https://{}:{}".format(*ADDR_PORT))
# Websocket server
wsd = websockets.serve(handler, *ADDR_PORT, ssl=sslctx, process_request=health_check,
                       # Maximum number of messages that websockets will pop
                       # off the asyncio and OS buffers per connection. See:
                       # https://websockets.readthedocs.io/en/stable/api.html#websockets.protocol.WebSocketCommonProtocol
                       max_queue=16)

logger = logging.getLogger('websockets.server')

logger.setLevel(logging.ERROR)
logger.addHandler(logging.StreamHandler())
asyncio.get_event_loop().run_until_complete(wsd)
asyncio.get_event_loop().run_forever()


from nintendo.common.transport import Socket
from nintendo.common.scheduler import Scheduler
from nintendo.common.crypto import RC4
from nintendo.common import util

import hashlib
import hmac
import struct
import random
import time

import logging
logger = logging.getLogger(__name__)

#These values are actually a bit more complicated,
#but since 0xA1 and 0xAF always work, there's no
#point in doing it differently.
PORT_SERVER = 0xA1
PORT_CLIENT = 0xAF

PACKET_SYN = 0
PACKET_CONNECT = 1
PACKET_DATA = 2
PACKET_DISCONNECT = 3
PACKET_PING = 4

FLAG_ACK = 0x10
FLAG_RELIABLE = 0x20
FLAG_NEED_ACK = 0x40
FLAG_80 = 0x80
FLAG_ACK2 = 0x2000

#Supported functions, unknown purpose
SUPPORT_2 = 2
SUPPORT_4 = 4
SUPPORT_100 = 0x100

#I ran into issues when I was missing a support
#flag, so I'm just setting all bits to 1 here
SUPPORT_ALL = 0xFFFFFFFF


class PRUDPError(ConnectionError): pass

def calc_server_signature(host, port, key):
	data = struct.pack("<IH", util.ip_to_hex(host), port)
	return hmac.HMAC(key, data).digest()
	
def calc_packet_signature(header, option, data, sig_sum, conn_sig, secure_key, sig_key):
	mac = hmac.HMAC(sig_key)
	mac.update(header[4:])
	mac.update(secure_key)
	mac.update(sig_sum)
	mac.update(conn_sig)
	mac.update(option)
	mac.update(data)
	return mac.digest()


class PacketOut:
	def __init__(self, type, flags, packet_id, data=b"", frag_id=0):
		self.type = type
		self.flags = flags
		self.packet_id = packet_id
		self.data = data
		self.frag_id = frag_id

	def encode(self, conn_id, server_sig, conn_sig, sig_sum, secure_key, sig_key):
		self.flags |= FLAG_80
		option = self.encode_option(server_sig)
		header = self.encode_header(len(option), conn_id)
		checksum = calc_packet_signature(header, option, self.data, sig_sum, conn_sig, secure_key, sig_key)
		return b"\xEA\xD0" + header + checksum + option + self.data
		
	def encode_option(self, server_sig):
		if self.type in [PACKET_SYN, PACKET_CONNECT]:
			data = b"\x00\x04"
			data += struct.pack("<I", SUPPORT_ALL)
			data += b"\x01\x10"
			if self.type == PACKET_SYN:
				data += b"\x00" * 0x10
			else: #PACKET_CONNECT
				data += server_sig
				data += b"\x03\x02"
				data += struct.pack("H", random.randint(0, 0xFFFF))
			data += b"\x04\x01\x00"
			return data
		elif self.type == PACKET_DATA:
			return bytes([self.type, 1, self.frag_id])
		return b""
		
	def encode_header(self, option_len, conn_id):
		data = b"\x01" #PRUDP Version
		data += struct.pack("B", option_len)
		data += struct.pack("<H", len(self.data))
		data += struct.pack("BB", PORT_CLIENT, PORT_SERVER)
		data += struct.pack("<H", self.type | self.flags)
		data += struct.pack("B", conn_id)
		data += struct.pack("B", 0)
		data += struct.pack("<H", self.packet_id)
		return data
		
	def __repr__(self):
		return "<PacketOut type=%i flags=%04X, id=%i>" %(self.type, self.flags, self.packet_id)
		
		
class PacketIn:
	def decode(self, data):	
		magic = data[:2]
		header = data[2 : 14]
		checksum = data[14 : 30]
		
		option_len, data_len = self.decode_header(header)
		option = data[30 : 30 + option_len]
		self.decode_option(option)
		
		self.data = data[30 + option_len : 30 + option_len + data_len]
		
	def decode_header(self, header):
		option_len = header[1]
		data_len = struct.unpack_from("<H", header, 2)[0]
		
		type_flags = struct.unpack_from("<H", header, 6)[0]
		self.type = type_flags & 0xF
		self.flags = type_flags & 0xFFF0
		
		self.chunk_id = header[9]
		self.packet_id = struct.unpack_from("<H", header, 10)[0]
		return option_len, data_len
		
	def decode_option(self, option):
		self.frag_id = 0
		if self.type == PACKET_SYN:
			self.conn_signature = option[8 : 24]
		elif self.type == PACKET_DATA:
			self.frag_id = option[2]
			
	def __repr__(self):
		return "<PacketIn type=%i flags=%04X, id=%i chunk=%i frag=%i>" %(self.type, self.flags, self.packet_id, self.chunk_id, self.frag_id)
		
		
class PRUDP:

	DISCONNECTED = 0
	CONNECTING = 1
	CONNECTED = 2
	DISCONNECTING = 3

	DEFAULT_KEY = b"CD&ML"
	
	connection_id = random.randint(0, 0xFF)

	def __init__(self, key):
		logger.debug("New client - access key=%s", key)
		self.encrypt = RC4(self.DEFAULT_KEY, False)
		self.decrypt = RC4(self.DEFAULT_KEY, False)
		
		self.signature_key = hashlib.md5(key).digest()
		self.signature_sum = struct.pack("<I", sum(key))

		self.frag_size = 1300
		self.resend_timeout = 2
		self.ping_timeout = 5
		self.silence_timeout = 8
		
		self.syn_packet = PacketOut(
			PACKET_SYN, FLAG_NEED_ACK, 0
		)
		
		self.connect_packet = PacketOut(
			PACKET_CONNECT, FLAG_RELIABLE | FLAG_NEED_ACK, 1
		)
		
		self.state = self.DISCONNECTED
		self.reset_connection()
		
	def set_secure_key(self, key):
		self.encrypt.set_key(key)
		self.decrypt.set_key(key)
		self.secure_key = key
		
	def reset_connection(self):
		self.s = Socket(Socket.UDP)
		self.set_state(self.DISCONNECTED)
		self.packet_id_out = 2
		self.packet_id_in = 1
		self.session_id = 0
		self.fragment_buffer = b""
		self.packet_queue = {}
		self.connection_signature = b"\x00" * 0x10

		self.ack_timers = {}
		self.ping_timer = 0
		self.silence_timer = 0
		
		self.secure_key = b""
		self.encrypt.set_key(self.DEFAULT_KEY)
		self.decrypt.set_key(self.DEFAULT_KEY)
		
	def set_state(self, state):
		if self.state != state:
			self.state = state
			self.on_state_change(state)
		
	def connect(self, host, port, data=b"", blocking=True):
		self.s.connect(host, port)
		self.server_signature = calc_server_signature(host, port, self.signature_key)
		self.connect_packet.data = data
		self.set_state(self.CONNECTING)
		
		Scheduler.instance.add(self.update)
		
		logger.debug("Connecting to %s:%i", host, port)
		
		self.send_packet(self.syn_packet)
		
		if blocking:
			while self.state == self.CONNECTING:
				time.sleep(0.05)
			if self.state != self.CONNECTED:
				raise PRUDPError("Failed to establish PRUDP connection to %s:%i" %(host, port))

	def send(self, data):
		frag_id = len(data) // self.frag_size
		while data:
			self.send_fragment(data[:self.frag_size], frag_id)
			data = data[self.frag_size:]
			frag_id -= 1

	def send_fragment(self, data, frag_id):
		encrypted = self.encrypt.crypt(data)
		packet = PacketOut(
			PACKET_DATA, FLAG_RELIABLE | FLAG_NEED_ACK, self.packet_id_out, encrypted, frag_id
		)
		self.packet_id_out += 1
		self.send_packet(packet)
		
	def close(self, blocking=True):
		self.set_state(self.DISCONNECTING)
		self.send_disconnect()
		
		if blocking:
			while self.state == self.DISCONNECTING:
				time.sleep(0.05)
				
	def send_disconnect(self):
		logger.debug("Sending DISCONNECT packet")
		packet = PacketOut(
			PACKET_DISCONNECT, FLAG_RELIABLE | FLAG_NEED_ACK, self.packet_id_out
		)
		self.send_packet(packet)
		
	def send_ping(self):
		logger.debug("Sending PING packet")
		packet = PacketOut(
			PACKET_PING, FLAG_RELIABLE | FLAG_NEED_ACK, self.packet_id_out
		)
		self.packet_id_out += 1
		self.send_packet(packet)
		
	def send_ack(self, packet):
		logger.debug("Sending ACK packet")
		packet = PacketOut(
			packet.type, FLAG_ACK, packet.packet_id, packet.data
		)
		self.send_packet(packet)
		
	def send_packet(self, packet):
		logger.debug("Sending packet: %s", packet)
		self.s.send(
			packet.encode(
				self.session_id, self.server_signature, self.connection_signature,
				self.signature_sum, self.secure_key, self.signature_key
			)
		)

		if packet.flags & FLAG_NEED_ACK:
			self.ack_timers[packet.packet_id] = [packet, 0]
			
	def handle_packet(self, packet):
		logger.debug("Processing packet: %s", packet)
		if packet.flags & FLAG_NEED_ACK:
			self.send_ack(packet)
	
		if packet.type == PACKET_DATA:
			self.fragment_buffer += self.decrypt.crypt(packet.data)
			if packet.frag_id == 0:
				self.on_data(self.fragment_buffer)
				self.fragment_buffer = b""
				
	def handle_ack(self, packet_id, packet):
		if packet_id in self.ack_timers:
			self.ack_timers.pop(packet_id)
			
		if packet.type == PACKET_SYN:
			self.connection_signature = packet.conn_signature
			
			self.session_id = PRUDP.connection_id
			PRUDP.connection_id = (PRUDP.connection_id + 1) & 0xFF
			
			self.send_packet(self.connect_packet)
		elif packet.type == PACKET_CONNECT:
			self.set_state(self.CONNECTED)
		elif packet.type == PACKET_DISCONNECT:
			Scheduler.instance.remove(self.update)
			self.reset_connection()
		
	def update(self, tick):
		for timer in self.ack_timers.values():
			timer[1] += tick
			if timer[1] >= self.resend_timeout:
				logger.info("Packet timed out, resending")
				self.send_packet(timer[0])
				
		self.ping_timer += tick
		if self.ping_timer >= self.ping_timeout:
			self.send_ping()
			self.ping_timer = 0
			
		self.silence_timer += tick
		if self.silence_timer >= self.silence_timeout:
			Scheduler.instance.remove(self.update)
			self.reset_connection()
			return
			
		#The real MTU is probably 1364, but to be safe, pass 1400
		data = self.s.recv(1400)
		if data:
			packet = PacketIn()
			packet.decode(data) #Maybe check checksum here?
			logger.debug("Packet received: %s", packet)
				
			if packet.flags & FLAG_ACK:
				self.handle_ack(packet.packet_id, packet)
				
			elif packet.flags & FLAG_ACK2:
				#This is weird
				packet_id = struct.unpack_from("<H", packet.data, 2 + packet.data[1] * 2)[0]
				self.handle_ack(packet_id, packet)
				
			else:
				self.packet_queue[packet.packet_id] = packet
				while self.packet_id_in in self.packet_queue:
					packet = self.packet_queue.pop(self.packet_id_in)
					self.handle_packet(packet)
					self.packet_id_in += 1
				
			self.ping_timer = 0
			self.silence_timer = 0
			
	def on_state_change(self, state): pass
	def on_data(self, data): pass

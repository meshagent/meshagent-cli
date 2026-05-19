import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:meshagent/meshagent.dart';

Future<void> main() async {
  final readyPath = Platform.environment['MESHAGENT_CREATE_DEV_READY_PATH'];
  final probe = Platform.environment['MESHAGENT_CREATE_DEV_PROBE'];
  final roomName = Platform.environment['MESHAGENT_ROOM'];
  final token = Platform.environment['MESHAGENT_TOKEN'];
  if (
    readyPath == null ||
    readyPath.isEmpty ||
    probe == null ||
    probe.isEmpty ||
    roomName == null ||
    roomName.isEmpty ||
    token == null ||
    token.isEmpty
  ) {
    return;
  }

  final room = RoomClient(
    protocolFactory: WebSocketClientProtocol.createFactory(
      url: _websocketRoomUrl(roomName),
      token: token,
    ),
  );
  await room.start();
  final payload = jsonEncode({
    'probe': probe,
    'room': room.roomName,
    'language': 'flutter',
    'focus': 'webserver',
  });
  await room.storage.upload(
    readyPath,
    Uint8List.fromList(utf8.encode('$payload\n')),
    overwrite: true,
    mimeType: 'application/json',
  );
  print('MeshAgent create dev probe wrote: $readyPath $probe');
  room.dispose();
}

Uri _websocketRoomUrl(String roomName) {
  var baseUrl = Platform.environment['MESHAGENT_ROOM_URL'] ??
      Platform.environment['MESHAGENT_API_URL'] ??
      'https://api.meshagent.com';
  if (baseUrl.startsWith('https:')) {
    baseUrl = 'wss:${baseUrl.substring('https:'.length)}';
  } else if (baseUrl.startsWith('http:')) {
    baseUrl = 'ws:${baseUrl.substring('http:'.length)}';
  }
  return Uri.parse('$baseUrl/rooms/$roomName');
}

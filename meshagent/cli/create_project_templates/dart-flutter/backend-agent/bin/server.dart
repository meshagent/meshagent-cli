import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:meshagent/meshagent.dart';

const proofDisplayPath = 'agent-proof.json';
const toolkitName = 'meshagent.create.dart-agent';

Future<void> main() async {
  final roomName = Platform.environment['MESHAGENT_ROOM'];
  final token = Platform.environment['MESHAGENT_TOKEN'];
  if (roomName == null || roomName.isEmpty || token == null || token.isEmpty) {
    print('MeshAgent room environment is not set; waiting for deployment env.');
    await Completer<void>().future;
    return;
  }

  final room = RoomClient(
    protocolFactory: WebSocketClientProtocol.createFactory(
      url: _websocketRoomUrl(roomName),
      token: token,
    ),
  );
  await room.start();
  print('Connected to MeshAgent room: ${room.roomName}');
  try {
    await runAgentToolkitProof(room);
  } finally {
    room.dispose();
  }
}

Future<void> runAgentToolkitProof(RoomClient room) async {
  final probe = Platform.environment['MESHAGENT_CREATE_DEV_PROBE'];
  final hostedToolkit = await startHostedToolkit(
    room: room,
    toolkit: DartAgentToolkit(),
    public: true,
  );
  try {
    if (probe == null || probe.isEmpty) {
      await room.waitForClose();
      return;
    }

    final pinged = await invokeJsonTool(room, 'ping', <String, dynamic>{});
    print('MeshAgent create dev toolkit ping: ${jsonEncode(pinged)}');

    final status = await invokeJsonTool(room, 'status', <String, dynamic>{});
    print('MeshAgent create dev toolkit status: ${jsonEncode(status)}');

    final message = 'MeshAgent local dev proof $probe';
    final echoed = await invokeJsonTool(room, 'echo', <String, dynamic>{
      'message': message,
    });
    print('MeshAgent create dev toolkit echo: ${jsonEncode(echoed)}');
    if (echoed['echo'] != message) {
      throw StateError('Local Dart agent toolkit proof did not echo the probe.');
    }

    await writeAgentProof(probe, message);
    print('MeshAgent create dev toolkit proof wrote: $proofDisplayPath $probe');

    final holdSeconds =
        double.tryParse(
          Platform.environment['MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS'] ??
              '0',
        ) ??
        0;
    if (holdSeconds > 0) {
      print(
        'MeshAgent create dev toolkit holding registration for ${holdSeconds}s',
      );
      await Future<void>.delayed(
        Duration(milliseconds: (holdSeconds * 1000).round()),
      );
    }
  } finally {
    await hostedToolkit.stop();
  }
}

Future<Map<String, dynamic>> invokeJsonTool(
  RoomClient room,
  String tool,
  Map<String, dynamic> arguments,
) async {
  final output = await room.agents.invokeTool(
    toolkit: toolkitName,
    tool: tool,
    input: ToolContentInput(JsonContent(json: arguments)),
  );
  if (output is! ToolContentOutput || output.content is! JsonContent) {
    throw StateError('Expected JSON response from $tool.');
  }
  return Map<String, dynamic>.from((output.content as JsonContent).json);
}

Future<void> writeAgentProof(String probe, String echo) async {
  final payload = {
    'probe': probe,
    'echo': echo,
    'tools': ['ping', 'status', 'echo'],
  };
  await File(proofDisplayPath).writeAsString(
    '${const JsonEncoder.withIndent('  ').convert(payload)}\n',
  );
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

class DartPingTool extends FunctionTool {
  DartPingTool()
      : super(
          name: 'ping',
          title: 'Ping local agent',
          description: 'Checks that the local Dart backend agent toolkit is reachable.',
          inputSchema: const {
            'type': 'object',
            'additionalProperties': false,
            'properties': <String, dynamic>{},
          },
        );

  @override
  Future<Content> execute(
    ToolContext context,
    Map<String, dynamic> arguments,
  ) async {
    return JsonContent(json: {'pong': true});
  }
}

class DartStatusTool extends FunctionTool {
  DartStatusTool()
      : super(
          name: 'status',
          title: 'Read local agent status',
          description: 'Returns a minimal status payload from the local Dart backend agent.',
          inputSchema: const {
            'type': 'object',
            'additionalProperties': false,
            'properties': <String, dynamic>{},
          },
        );

  @override
  Future<Content> execute(
    ToolContext context,
    Map<String, dynamic> arguments,
  ) async {
    return JsonContent(
      json: {'ready': true, 'language': 'dart', 'focus': 'backend-agent'},
    );
  }
}

class DartEchoTool extends FunctionTool {
  DartEchoTool()
      : super(
          name: 'echo',
          title: 'Echo a message',
          description: 'Echoes a message through the local Dart backend agent.',
          inputSchema: const {
            'type': 'object',
            'required': ['message'],
            'additionalProperties': false,
            'properties': {
              'message': {'type': 'string'},
            },
          },
        );

  @override
  Future<Content> execute(
    ToolContext context,
    Map<String, dynamic> arguments,
  ) async {
    final message = arguments['message'];
    if (message is! String) {
      throw ArgumentError('message must be a string');
    }
    return JsonContent(json: {'echo': message});
  }
}

class DartAgentToolkit extends Toolkit {
  DartAgentToolkit()
      : super(
          name: toolkitName,
          title: 'Dart Local Agent Toolkit',
          description: 'Local-only ping, status, and echo tools for the Dart backend agent.',
          tools: [DartPingTool(), DartStatusTool(), DartEchoTool()],
        );
}

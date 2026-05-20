using System.Globalization;
using System.Text.Json;
using Meshagent.Api.Messaging;
using Meshagent.Api.Protocol;
using Meshagent.Api.Room;

const string ProofDisplayPath = "agent-proof.json";

if (
    string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MESHAGENT_ROOM"))
    || string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MESHAGENT_TOKEN"))
)
{
    Console.WriteLine("MeshAgent room environment is not set; waiting for deployment env.");
    await Task.Delay(Timeout.InfiniteTimeSpan);
    return;
}

await using var room = new RoomClient();
await room.ConnectAsync();
Console.WriteLine($"Connected to MeshAgent room: {room.RoomName}");
await RunAgentToolkitProofAsync(room);

static async Task RunAgentToolkitProofAsync(RoomClient room)
{
    var probe = Environment.GetEnvironmentVariable("MESHAGENT_CREATE_DEV_PROBE");
    var hostedToolkit = await DotNetAgentToolkitHost.StartAsync(room);
    try
    {
        if (string.IsNullOrWhiteSpace(probe))
        {
            await Task.Delay(Timeout.InfiniteTimeSpan);
            return;
        }

        var pinged = await InvokeJsonToolAsync(room, "ping", new Dictionary<string, object?>());
        Console.WriteLine($"MeshAgent create dev toolkit ping: {JsonSerializer.Serialize(pinged)}");

        var status = await InvokeJsonToolAsync(room, "status", new Dictionary<string, object?>());
        Console.WriteLine($"MeshAgent create dev toolkit status: {JsonSerializer.Serialize(status)}");

        var message = $"MeshAgent local dev proof {probe}";
        var echoed = await InvokeJsonToolAsync(
            room,
            "echo",
            new Dictionary<string, object?> { ["message"] = message });
        Console.WriteLine($"MeshAgent create dev toolkit echo: {JsonSerializer.Serialize(echoed)}");
        if (!Equals(echoed.GetValueOrDefault("echo"), message))
        {
            throw new InvalidOperationException("Local .NET agent toolkit proof did not echo the probe.");
        }

        await WriteAgentProofAsync(probe, message);
        Console.WriteLine($"MeshAgent create dev toolkit proof wrote: {ProofDisplayPath} {probe}");

        var holdSecondsRaw = Environment.GetEnvironmentVariable("MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS") ?? "0";
        if (double.TryParse(holdSecondsRaw, NumberStyles.Float, CultureInfo.InvariantCulture, out var holdSeconds) && holdSeconds > 0)
        {
            Console.WriteLine($"MeshAgent create dev toolkit holding registration for {holdSeconds}s");
            await Task.Delay(TimeSpan.FromSeconds(holdSeconds));
        }
    }
    finally
    {
        await hostedToolkit.StopAsync();
    }
}

static async Task<Dictionary<string, object?>> InvokeJsonToolAsync(
    RoomClient room,
    string tool,
    Dictionary<string, object?> arguments)
{
    var result = await room.Agents.InvokeTool(DotNetAgentToolkitHost.ToolkitName, tool, arguments);
    if (result is not JsonChunk json)
    {
        throw new InvalidOperationException($"Expected JSON response from {tool}.");
    }
    return json.Json;
}

static async Task WriteAgentProofAsync(string probe, string echo)
{
    var payload = new Dictionary<string, object?>
    {
        ["probe"] = probe,
        ["echo"] = echo,
        ["tools"] = new[] { "ping", "status", "echo" },
    };
    var json = JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true });
    await File.WriteAllTextAsync(Path.Combine(Directory.GetCurrentDirectory(), ProofDisplayPath), json + "\n");
}

sealed class DotNetAgentToolkitHost
{
    public const string ToolkitName = "meshagent.create.dotnet-agent";

    private readonly RoomClient _room;
    private readonly Func<Protocol, long, string, byte[], Task> _toolCallHandler;
    private string? _registrationId;
    private bool _started;

    private DotNetAgentToolkitHost(RoomClient room)
    {
        _room = room;
        _toolCallHandler = HandleToolCallAsync;
    }

    public static async Task<DotNetAgentToolkitHost> StartAsync(RoomClient room)
    {
        var host = new DotNetAgentToolkitHost(room);
        room.Protocol.RegisterHandler($"room.tool_call.{ToolkitName}", host._toolCallHandler);
        try
        {
            await host.RegisterAsync();
            host._started = true;
            return host;
        }
        catch
        {
            room.Protocol.UnregisterHandler($"room.tool_call.{ToolkitName}", host._toolCallHandler);
            throw;
        }
    }

    public async Task StopAsync()
    {
        if (!_started)
        {
            return;
        }

        _started = false;
        try
        {
            if (!string.IsNullOrWhiteSpace(_registrationId) && !_room.IsClosed)
            {
                await _room.SendRequest(
                    "room.unregister_toolkit",
                    new Dictionary<string, object?> { ["id"] = _registrationId });
            }
        }
        finally
        {
            _room.Protocol.UnregisterHandler($"room.tool_call.{ToolkitName}", _toolCallHandler);
        }
    }

    private async Task RegisterAsync()
    {
        var response = await _room.SendRequest(
            "room.register_toolkit",
            new Dictionary<string, object?>
            {
                ["name"] = ToolkitName,
                ["title"] = ".NET Local Agent Toolkit",
                ["description"] = "Local-only ping, status, and echo tools for the .NET backend agent.",
                ["tools"] = ToolDescriptions(),
                ["public"] = true,
            });

        if (response is JsonChunk json && json.Json.TryGetValue("id", out var id))
        {
            _registrationId = id?.ToString();
        }
    }

    private async Task HandleToolCallAsync(Protocol protocol, long messageId, string messageType, byte[] data)
    {
        try
        {
            var unpacked = MessageUtil.UnpackMessage(data);
            var message = unpacked.header;
            var toolName = message.TryGetValue("name", out var rawName) ? rawName as string : null;
            var arguments = DecodeArguments(message.TryGetValue("arguments", out var rawArguments) ? rawArguments : null);

            Chunk response = toolName switch
            {
                "ping" => new JsonChunk(new Dictionary<string, object?> { ["pong"] = true }),
                "status" => new JsonChunk(new Dictionary<string, object?>
                {
                    ["ready"] = true,
                    ["language"] = "dotnet",
                    ["focus"] = "backend-agent",
                }),
                "echo" => new JsonChunk(new Dictionary<string, object?>
                {
                    ["echo"] = arguments.GetValueOrDefault("message") as string ?? "",
                }),
                _ => new ErrorChunk($"Unknown tool: {toolName ?? "(missing)"}"),
            };

            await protocol.Send("room.tool_call_response", response.Pack(), messageId);
        }
        catch (Exception ex)
        {
            await protocol.Send("room.tool_call_response", new ErrorChunk(ex.Message).Pack(), messageId);
        }
    }

    private static Dictionary<string, object?> DecodeArguments(object? rawArguments)
    {
        if (rawArguments is not IDictionary<string, object?> rawMap)
        {
            return new Dictionary<string, object?>();
        }

        var arguments = new Dictionary<string, object?>(rawMap);
        if (
            Equals(arguments.GetValueOrDefault("type"), "json")
            && arguments.GetValueOrDefault("json") is IDictionary<string, object?> jsonMap
        )
        {
            return new Dictionary<string, object?>(jsonMap);
        }

        return arguments;
    }

    private static Dictionary<string, object?> ToolDescriptions()
    {
        return new Dictionary<string, object?>
        {
            ["ping"] = ToolDescription(
                "Ping local agent",
                "Checks that the local .NET backend agent toolkit is reachable.",
                EmptyInputSchema()),
            ["status"] = ToolDescription(
                "Read local agent status",
                "Returns a minimal status payload from the local .NET backend agent.",
                EmptyInputSchema()),
            ["echo"] = ToolDescription(
                "Echo a message",
                "Echoes a message through the local .NET backend agent.",
                new Dictionary<string, object?>
                {
                    ["type"] = "object",
                    ["required"] = new[] { "message" },
                    ["additionalProperties"] = false,
                    ["properties"] = new Dictionary<string, object?>
                    {
                        ["message"] = new Dictionary<string, object?> { ["type"] = "string" },
                    },
                }),
        };
    }

    private static Dictionary<string, object?> ToolDescription(
        string title,
        string description,
        Dictionary<string, object?> inputSchema)
    {
        return new Dictionary<string, object?>
        {
            ["title"] = title,
            ["description"] = description,
            ["input_spec"] = new Dictionary<string, object?>
            {
                ["types"] = new[] { "json" },
                ["stream"] = false,
                ["schema"] = inputSchema,
            },
            ["output_spec"] = null,
            ["defs"] = null,
            ["strict"] = null,
        };
    }

    private static Dictionary<string, object?> EmptyInputSchema()
    {
        return new Dictionary<string, object?>
        {
            ["type"] = "object",
            ["additionalProperties"] = false,
            ["properties"] = new Dictionary<string, object?>(),
        };
    }
}

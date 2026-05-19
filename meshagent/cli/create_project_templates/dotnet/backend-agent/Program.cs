using System.Text;
using System.Text.Json;
using Meshagent.Api.Room;

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
await PublishDevReadyMarkerAsync(room);
await Task.Delay(Timeout.InfiniteTimeSpan);

static async Task PublishDevReadyMarkerAsync(RoomClient room)
{
    var readyPath = Environment.GetEnvironmentVariable("MESHAGENT_CREATE_DEV_READY_PATH");
    var probe = Environment.GetEnvironmentVariable("MESHAGENT_CREATE_DEV_PROBE");
    if (string.IsNullOrWhiteSpace(readyPath) || string.IsNullOrWhiteSpace(probe))
    {
        return;
    }

    var payload = JsonSerializer.Serialize(new
    {
        probe,
        room = room.RoomName,
        language = "dotnet",
        focus = "backend-agent",
    }) + "\n";
    await room.Storage.Upload(
        readyPath,
        Encoding.UTF8.GetBytes(payload),
        overwrite: true,
        mimeType: "application/json");
    Console.WriteLine($"MeshAgent create dev probe wrote: {readyPath} {probe}");
}

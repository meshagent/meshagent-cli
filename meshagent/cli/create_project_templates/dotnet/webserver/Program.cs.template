using System.Text;
using System.Text.Json;
using Meshagent.Api.Room;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

var port = Environment.GetEnvironmentVariable("PORT") ?? "5000";
app.Urls.Clear();
app.Urls.Add($"http://0.0.0.0:{port}");

app.MapGet("/health", () => Results.Text("ok\n", "text/plain"));
app.MapGet("/status", () => Results.Text("ready\n", "text/plain"));
app.MapGet("/api/ping", () => Results.Json(new { pong = true }));
app.MapGet("/", () => Results.Text("hello from meshagent create\n", "text/plain"));

_ = PublishDevReadyMarkerAsync("webserver");

await app.RunAsync();

static async Task PublishDevReadyMarkerAsync(string focus)
{
    var readyPath = Environment.GetEnvironmentVariable("MESHAGENT_CREATE_DEV_READY_PATH");
    var probe = Environment.GetEnvironmentVariable("MESHAGENT_CREATE_DEV_PROBE");
    if (
        string.IsNullOrWhiteSpace(readyPath)
        || string.IsNullOrWhiteSpace(probe)
        || string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MESHAGENT_ROOM"))
        || string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MESHAGENT_TOKEN"))
    )
    {
        return;
    }

    await using var room = new RoomClient();
    await room.ConnectAsync();
    var payload = JsonSerializer.Serialize(new
    {
        probe,
        room = room.RoomName,
        language = "dotnet",
        focus,
    }) + "\n";
    await room.Storage.Upload(
        readyPath,
        Encoding.UTF8.GetBytes(payload),
        overwrite: true,
        mimeType: "application/json");
    Console.WriteLine($"MeshAgent create dev probe wrote: {readyPath} {probe}");
    await room.WaitForCloseAsync();
}

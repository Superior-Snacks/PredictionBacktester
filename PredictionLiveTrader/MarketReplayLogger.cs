using System;
using System.IO;
using System.IO.Compression;
using System.Threading.Channels;
using System.Threading.Tasks;

public class MarketReplayLogger
{
    private readonly Channel<string> _logQueue;
    private readonly string _logDirectory;
    private readonly string _sessionId;
    private readonly Task _processTask;
    private int _ticksSinceFlush;
    private const int FlushInterval = 5000;

    public MarketReplayLogger(string directoryPath = "MarketData")
    {
        _logDirectory = directoryPath;
        _sessionId = DateTime.UtcNow.ToString("yyyyMMdd_HHmmss");

        if (!Directory.Exists(_logDirectory))
        {
            Directory.CreateDirectory(_logDirectory);
        }

        _logQueue = Channel.CreateUnbounded<string>(new UnboundedChannelOptions
        {
            SingleReader = true
        });

        _processTask = Task.Run(ProcessLogQueueAsync);
    }

    public void EnqueueTick(string rawJsonMessage)
    {
        var tickData = $"{DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()}|{rawJsonMessage}";
        _logQueue.Writer.TryWrite(tickData);
    }

    public async Task StopAsync()
    {
        _logQueue.Writer.Complete();
        await _processTask;
    }

    private async Task ProcessLogQueueAsync()
    {
        string currentDate = "";
        StreamWriter? writer = null;
        GZipStream? gzStream = null;
        FileStream? fileStream = null;

        try
        {
            await foreach (var tick in _logQueue.Reader.ReadAllAsync())
            {
                string today = DateTime.UtcNow.ToString("yyyyMMdd");

                if (currentDate != today)
                {
                    // Flush and close the previous day's file
                    if (writer != null)
                    {
                        await writer.FlushAsync();
                        await writer.DisposeAsync();
                        if (gzStream != null) await gzStream.DisposeAsync();
                        if (fileStream != null) await fileStream.DisposeAsync();
                    }

                    // New file per session + day (e.g., "L2_20260317_143022.gz")
                    currentDate = today;
                    string filename = Path.Combine(_logDirectory, $"L2_{today}_{_sessionId.Split('_')[1]}.gz");
                    fileStream = new FileStream(filename, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
                    gzStream = new GZipStream(fileStream, CompressionLevel.SmallestSize);
                    writer = new StreamWriter(gzStream);
                    _ticksSinceFlush = 0;
                }

                await writer!.WriteLineAsync(tick);

                if (++_ticksSinceFlush >= FlushInterval)
                {
                    await writer.FlushAsync();
                    _ticksSinceFlush = 0;
                }
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[LOGGER FATAL ERROR] {ex.Message}");
        }
        finally
        {
            // Final flush to capture remaining buffered ticks
            if (writer != null)
            {
                await writer.FlushAsync();
                await writer.DisposeAsync();
            }
            if (gzStream != null) await gzStream.DisposeAsync();
            if (fileStream != null) await fileStream.DisposeAsync();
        }
    }
}
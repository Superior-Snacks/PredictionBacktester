using System.Globalization;

namespace HardVenArb;

/// <summary>
/// The LIVE account-currency→USD rate, shared by everything that converts between the book's money (Pinnacle
/// = EUR) and the executor's USD-payout contracts.
///
/// <para><b>Why this is not just an env var.</b> It was one — <c>HARDVEN_FX_TO_USD</c> — and on 2026-08-06 it
/// read 1.08 while EUR/USD was 1.1542 (6.9% stale). Because the book stake is <c>stakeUSD / fx</c>, too low a
/// rate over-stakes the book leg: the two legs stop paying the same amount and the hedge silently becomes a
/// DIRECTIONAL position, while the arb detection (which is unitless, and correct) still reports a clean edge.
/// A number that must be right and is maintained by hand will eventually be wrong, so the sidecar now fetches
/// it and this holder distributes it.</para>
///
/// <para><b>Safety.</b> Seeded from the env at startup so behaviour is unchanged if the sidecar never answers.
/// Updates are rejected unless they are positive and within <see cref="MaxDeviation"/> of the seed, so a
/// mangled response can never resize real bets. Reads are lock-guarded because <c>decimal</c> is 16 bytes and
/// therefore not atomic — a torn read here would mis-size a live order.</para>
/// </summary>
public static class FxRate
{
    private static readonly object _lock = new();
    private static decimal _rate = 1.0m;
    private static decimal _seed = 1.0m;
    private static string _source = "default";
    private static DateTime _updatedUtc = DateTime.MinValue;

    /// <summary>Max fractional distance from the seed an update may be (env HARDVEN_FX_MAX_DEVIATION, default 0.25).</summary>
    public static decimal MaxDeviation { get; private set; } = 0.25m;

    /// <summary>USD per one unit of the book's account currency. 1.0 = USD book / no conversion.</summary>
    public static decimal Current { get { lock (_lock) return _rate; } }

    public static string Source { get { lock (_lock) return _source; } }

    /// <summary>Age of the last accepted update; <see cref="TimeSpan.MaxValue"/> if never updated.</summary>
    public static TimeSpan Age
    {
        get { lock (_lock) return _updatedUtc == DateTime.MinValue ? TimeSpan.MaxValue : DateTime.UtcNow - _updatedUtc; }
    }

    /// <summary>Seed from the environment (HARDVEN_FX_TO_USD). Call once at startup, before any sizing.</summary>
    public static void SeedFromEnvironment()
    {
        decimal v = decimal.TryParse(Environment.GetEnvironmentVariable("HARDVEN_FX_TO_USD"),
            NumberStyles.Any, CultureInfo.InvariantCulture, out var e) && e > 0m ? e : 1.0m;
        if (decimal.TryParse(Environment.GetEnvironmentVariable("HARDVEN_FX_MAX_DEVIATION"),
                NumberStyles.Any, CultureInfo.InvariantCulture, out var d) && d > 0m && d < 1m)
            MaxDeviation = d;
        lock (_lock) { _rate = _seed = v; _source = "env"; }
    }

    /// <summary>Apply a fetched rate. Returns false (and changes nothing) when it is non-positive or outside
    /// the sanity band around the seed — failing to the configured value, never to a guess.</summary>
    public static bool TryUpdate(decimal rate, string source, out string reason)
    {
        reason = "";
        if (rate <= 0m) { reason = "non-positive"; return false; }
        decimal seed;
        lock (_lock) seed = _seed;
        if (seed > 0m && Math.Abs(rate - seed) / seed > MaxDeviation)
        {
            reason = $"{rate:0.0000} deviates >{MaxDeviation:P0} from the seed {seed:0.0000}";
            return false;
        }
        lock (_lock) { _rate = rate; _source = source; _updatedUtc = DateTime.UtcNow; }
        return true;
    }

    /// <summary>One-line state for the status/heartbeat output.</summary>
    public static string Describe()
    {
        lock (_lock)
        {
            string age = _updatedUtc == DateTime.MinValue ? "never"
                       : $"{(DateTime.UtcNow - _updatedUtc).TotalMinutes:0}m ago";
            string drift = _seed > 0m ? $", seed {_seed:0.0000} ({(_rate / _seed - 1m):+0.0%;-0.0%;0.0%})" : "";
            return $"fx={_rate:0.0000} ({_source}, {age}{drift})";
        }
    }
}

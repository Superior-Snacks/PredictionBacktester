using System.Threading.Tasks;

namespace PredictionBacktester.Strategies.Sweepers
{
    public interface IStrategySweeper
    {
        Task RunSweepAsync();
    }
}

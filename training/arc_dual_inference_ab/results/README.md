# Synthetic result records

`synthetic_mock_20x5.json` and `synthetic_mock_20x5_trs.json` contain 20 paired seeds over five scenarios, with TRS disabled/enabled in A respectively. The two modelled mean-latency comparisons differ because the TRS branch adds the mock tie-breaker delay. Both records say `quality_status: not_measured`, `gpu_calls: 0`, `network_calls: 0`, and `weights_changed: false`. Scheduler wall-time variation is not model inference latency.

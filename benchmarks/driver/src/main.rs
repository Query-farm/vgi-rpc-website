//! Fixed-client benchmark driver for the cross-language release matrix.
//!
//! The process emits machine-readable evidence only. Presentation, release
//! provenance, and publish policy live in the website's Python orchestrator.

use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Barrier, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use arrow_array::{Array, Float64Array, LargeBinaryArray, RecordBatch, StringArray};
use arrow_schema::{DataType, Field, Schema};
use serde::{Deserialize, Serialize};
use serde_json::json;
use vgi_rpc::wire::Metadata;
use vgi_rpc_client::{HttpClient, RpcClient};

const TRANSPORTS: [&str; 6] = [
    "stdio",
    "unix",
    "tcp",
    "http_identity",
    "http_zstd",
    "stdio_shm",
];
const LOAD_CONCURRENCY: [usize; 5] = [1, 2, 4, 8, 16];
const SEED: u64 = 1_447_316_557;
static CHECKPOINT_PATH: OnceLock<PathBuf> = OnceLock::new();
static PROGRESS_START: OnceLock<Instant> = OnceLock::new();
static PROGRESS_TOTAL: AtomicUsize = AtomicUsize::new(0);
static PROGRESS_COMPLETED: AtomicUsize = AtomicUsize::new(0);
static LISTENER_SEQUENCE: AtomicUsize = AtomicUsize::new(0);

#[derive(Deserialize)]
struct Config {
    id: String,
    name: String,
    version: String,
    sha: String,
    commands: BTreeMap<String, Option<Vec<String>>>,
}

#[derive(Clone, Serialize)]
struct RoundSummary {
    sample_count: usize,
    duration_ns: u64,
    p50_ns: f64,
    p95_ns: f64,
    p99_ns: f64,
    min_ns: u64,
    max_ns: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    cpu_utilization_percent: Option<f64>,
}

#[derive(Serialize)]
struct ResultRow {
    layer: &'static str,
    client: &'static str,
    server: String,
    transport: String,
    workload: String,
    concurrency: usize,
    status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    payload_bytes: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    sample_count: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    mean_ns: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    p50_ns: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    p95_ns: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    p99_ns: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    operations_per_second: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    payload_bytes_per_second: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    cpu_utilization_percent: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    round_cv: Option<f64>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    rounds: Vec<RoundSummary>,
}

struct Measurement {
    rounds: Vec<RoundSummary>,
    samples: Vec<u64>,
    elapsed_ns: u64,
}

struct ChildGuard(Child);

impl Drop for ChildGuard {
    fn drop(&mut self) {
        #[cfg(unix)]
        unsafe {
            // The listener is placed in its own process group at spawn time.
            // Kill the group so wrappers such as `uv run` cannot leak workers.
            libc::kill(-(self.0.id() as i32), libc::SIGKILL);
        }
        #[cfg(not(unix))]
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[allow(clippy::large_enum_variant)]
enum Connection {
    Bytes {
        client: RpcClient,
        _listener: Option<ChildGuard>,
    },
    Http {
        client: HttpClient,
        _listener: ChildGuard,
    },
}

enum LoadConnection {
    Bytes(RpcClient),
    Http(Box<HttpClient>),
}

impl LoadConnection {
    fn call(
        &mut self,
        method: &str,
        params: &RecordBatch,
    ) -> Result<(RecordBatch, Metadata), String> {
        let result = match self {
            LoadConnection::Bytes(client) => client.call_unary(method, params, None),
            LoadConnection::Http(client) => client.call_unary(method, params, None),
        };
        result.map_err(|error| error.to_string())
    }
}

impl Connection {
    fn call(
        &mut self,
        method: &str,
        params: &RecordBatch,
    ) -> Result<(RecordBatch, Metadata), String> {
        let result = match self {
            Connection::Bytes { client, .. } => client.call_unary(method, params, None),
            Connection::Http { client, .. } => client.call_unary(method, params, None),
        };
        result.map_err(|error| error.to_string())
    }
}

fn main() {
    let mut config_path = None;
    let mut output_path = None;
    let mut mode = String::from("quick");
    let mut suite = String::from("all");
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--config" => {
                config_path = args.get(index + 1).cloned();
                index += 2;
            }
            "--output" => {
                output_path = args.get(index + 1).cloned();
                index += 2;
            }
            "--mode" => {
                mode = args.get(index + 1).cloned().unwrap_or_default();
                index += 2;
            }
            "--suite" => {
                suite = args.get(index + 1).cloned().unwrap_or_default();
                index += 2;
            }
            unknown => fail(&format!("unknown argument: {unknown}")),
        }
    }
    if mode != "quick" && mode != "full" {
        fail("--mode must be quick or full");
    }
    if !matches!(suite.as_str(), "all" | "latency" | "scaling") {
        fail("--suite must be all, latency, or scaling");
    }
    let config_path = config_path.unwrap_or_else(|| fail("--config is required"));
    let output_path = output_path.unwrap_or_else(|| fail("--output is required"));
    let config: Config =
        serde_json::from_slice(&fs::read(config_path).unwrap_or_else(|e| fail(&e.to_string())))
            .unwrap_or_else(|e| fail(&e.to_string()));
    let checkpoint_path = PathBuf::from(format!("{output_path}.jsonl"));
    let _ = fs::remove_file(&checkpoint_path);
    CHECKPOINT_PATH
        .set(checkpoint_path)
        .unwrap_or_else(|_| fail("checkpoint path was already initialized"));
    PROGRESS_START
        .set(Instant::now())
        .unwrap_or_else(|_| fail("progress timer was already initialized"));
    let supported_load_transports = config
        .commands
        .iter()
        .filter(|(transport, command)| {
            matches!(
                transport.as_str(),
                "unix" | "tcp" | "http_identity" | "http_zstd"
            ) && command.is_some()
        })
        .count();
    let latency_scenarios = if suite != "scaling" {
        TRANSPORTS.len() * workload_names().len()
    } else {
        0
    };
    let scaling_scenarios = if suite != "latency" {
        supported_load_transports * load_workload_names().len() * LOAD_CONCURRENCY.len()
    } else {
        0
    };
    PROGRESS_TOTAL.store(latency_scenarios + scaling_scenarios, Ordering::Relaxed);

    eprintln!(
        "benchmarking {} {} at {}",
        config.name, config.version, config.sha
    );
    let mut results = Vec::new();
    let order = shuffled_transports();
    for transport in order {
        let command = config.commands.get(transport).cloned().flatten();
        if command.is_none() {
            if suite != "scaling" {
                for (workload, payload) in workload_names() {
                    results.push(unsupported(&config.id, transport, workload, payload));
                }
            }
            continue;
        }
        eprintln!("  {transport}");
        let command = command.unwrap();
        if suite != "scaling" {
            let mut rows = benchmark_transport(&config, transport, command.clone(), &mode);
            results.append(&mut rows);
        }
        if suite != "latency" && matches!(transport, "unix" | "tcp" | "http_identity" | "http_zstd")
        {
            let mut load_rows = benchmark_load(&config, transport, command, &mode);
            results.append(&mut load_rows);
        }
    }
    let document =
        json!({"schema_version": 1, "server": config.id, "suite": suite, "results": results});
    fs::write(output_path, serde_json::to_vec_pretty(&document).unwrap())
        .unwrap_or_else(|e| fail(&e.to_string()));
}

fn benchmark_transport(
    config: &Config,
    transport: &str,
    command: Vec<String>,
    mode: &str,
) -> Vec<ResultRow> {
    let mut connection = match connect(transport, command) {
        Ok(connection) => connection,
        Err(error) => {
            eprintln!("    failed to connect {transport}: {error}");
            return workload_names()
                .into_iter()
                .map(|(workload, payload)| failed(&config.id, transport, workload, payload, &error))
                .collect();
        }
    };
    let (warmup, duration, rounds) = if mode == "quick" {
        (Duration::ZERO, Duration::ZERO, 1)
    } else {
        (Duration::from_secs(1), Duration::from_secs(1), 5)
    };
    let mut rows = Vec::new();
    let unary = vec![
        ("void_noop", "void_noop", empty_batch()),
        (
            "add_floats",
            "add_floats",
            batch_f64(&[("a", 1.0), ("b", 2.0)]),
        ),
        (
            "echo_string_11b",
            "echo_string",
            batch_string("value", "hello world"),
        ),
    ];
    for (label, method, params) in unary {
        let measurement = measure(
            warmup,
            duration,
            rounds,
            if mode == "quick" { 2 } else { 100 },
            || {
                let (batch, _) = connection.call(method, &params)?;
                validate_unary(label, &batch)
            },
        );
        match measurement {
            Ok(measurement) => {
                rows.push(measured(&config.id, transport, label, None, 1, measurement))
            }
            Err(error) => {
                eprintln!("    failed latency {transport}/{label}: {error}");
                rows.push(failed(
                    &config.id,
                    transport,
                    label.to_string(),
                    None,
                    &error,
                ));
            }
        }
    }
    for size in [1usize << 10, 64usize << 10, 1usize << 20, 16usize << 20] {
        let params = payload_batch(size);
        let minimum = if mode == "quick" {
            1
        } else if size == 16 << 20 {
            5
        } else {
            100
        };
        let measurement = measure(warmup, duration, rounds, minimum, || {
            let (batch, metadata) = connection.call("echo_large_binary", &params)?;
            validate_payload(&batch, size)?;
            if transport == "stdio_shm"
                && size >= 1 << 20
                && !metadata.contains_key(vgi_rpc::metadata::SHM_SOURCE_KEY)
            {
                return Err("large payload silently fell back from shared memory".to_string());
            }
            Ok(())
        });
        let workload = format!("echo_binary_{size}");
        match measurement {
            Ok(measurement) => rows.push(measured(
                &config.id,
                transport,
                &workload,
                Some(size),
                1,
                measurement,
            )),
            Err(error) => {
                eprintln!("    failed latency {transport}/{workload}: {error}");
                rows.push(failed(&config.id, transport, workload, Some(size), &error));
            }
        }
    }
    rows
}

fn benchmark_load(
    config: &Config,
    transport: &str,
    command: Vec<String>,
    mode: &str,
) -> Vec<ResultRow> {
    let (warmup, duration, base_rounds) = if mode == "quick" {
        (Duration::ZERO, Duration::ZERO, 1)
    } else {
        (Duration::from_secs(1), Duration::from_secs(2), 3)
    };
    let mut rows = Vec::new();
    for (workload, payload_bytes) in load_workload_names() {
        for concurrency in LOAD_CONCURRENCY {
            let scenario = (|| -> Result<ResultRow, String> {
                // A fresh listener isolates each scaling point and avoids connection
                // teardown from one concurrency level affecting the next one.
                let (target, _listener) = launch_listener(transport, command.clone())?;
                if !warmup.is_zero() {
                    load_window(transport, &target, &workload, concurrency, warmup, 1)?;
                }
                let mut summaries = Vec::new();
                let mut samples = Vec::new();
                let mut elapsed_ns = 0u64;
                let mut target_rounds = base_rounds;
                let mut round_index = 0;
                while round_index < target_rounds {
                    let (round_samples, round_duration_ns, cpu_utilization_percent) =
                        load_window(transport, &target, &workload, concurrency, duration, 1)?;
                    elapsed_ns += round_duration_ns;
                    let mut summary = summarize(&round_samples, round_duration_ns);
                    summary.cpu_utilization_percent = cpu_utilization_percent;
                    summaries.push(summary);
                    samples.extend(round_samples);
                    round_index += 1;
                    if round_index == base_rounds
                        && base_rounds > 1
                        && cv(&summaries
                            .iter()
                            .map(|round| round.p50_ns)
                            .collect::<Vec<_>>())
                            > 0.05
                    {
                        target_rounds = 5;
                    }
                }
                Ok(measured(
                    &config.id,
                    transport,
                    &workload,
                    payload_bytes,
                    concurrency,
                    Measurement {
                        rounds: summaries,
                        samples,
                        elapsed_ns,
                    },
                ))
            })();
            match scenario {
                Ok(row) => rows.push(row),
                Err(error) => {
                    eprintln!("    failed {transport}/{workload}/c={concurrency}: {error}");
                    rows.push(failed_with_concurrency(
                        &config.id,
                        transport,
                        workload.clone(),
                        payload_bytes,
                        concurrency,
                        &error,
                    ));
                }
            }
        }
    }
    rows
}

fn load_window(
    transport: &str,
    target: &str,
    workload: &str,
    concurrency: usize,
    minimum_duration: Duration,
    minimum_samples_per_client: usize,
) -> Result<(Vec<u64>, u64, Option<f64>), String> {
    let mut connections = Vec::with_capacity(concurrency);
    for _ in 0..concurrency {
        connections.push(connect_target(transport, target)?);
    }
    let barrier = Arc::new(Barrier::new(concurrency + 1));
    let mut handles = Vec::with_capacity(concurrency);
    for mut connection in connections {
        let barrier = Arc::clone(&barrier);
        let workload = workload.to_string();
        handles.push(thread::spawn(move || -> Result<Vec<u64>, String> {
            let params = match workload.as_str() {
                "add_floats_load" => batch_f64(&[("a", 1.0), ("b", 2.0)]),
                "echo_binary_1048576_load" => payload_batch(1 << 20),
                other => return Err(format!("unknown load workload: {other}")),
            };
            barrier.wait();
            let window_start = Instant::now();
            let mut samples = Vec::new();
            while samples.len() < minimum_samples_per_client
                || window_start.elapsed() < minimum_duration
            {
                let call_start = Instant::now();
                let (batch, _) = match workload.as_str() {
                    "add_floats_load" => connection.call("add_floats", &params)?,
                    "echo_binary_1048576_load" => connection.call("echo_large_binary", &params)?,
                    _ => unreachable!(),
                };
                samples.push(call_start.elapsed().as_nanos() as u64);
                match workload.as_str() {
                    "add_floats_load" => validate_unary("add_floats", &batch)?,
                    "echo_binary_1048576_load" => validate_payload(&batch, 1 << 20)?,
                    _ => unreachable!(),
                }
            }
            Ok(samples)
        }));
    }
    let cpu_start = cpu_times();
    barrier.wait();
    let window_start = Instant::now();
    let mut samples = Vec::new();
    for handle in handles {
        let mut thread_samples = handle
            .join()
            .map_err(|_| "load client thread panicked".to_string())??;
        samples.append(&mut thread_samples);
    }
    let duration_ns = window_start.elapsed().as_nanos() as u64;
    let cpu_utilization_percent = cpu_start
        .zip(cpu_times())
        .and_then(|(start, end)| cpu_utilization(start, end));
    Ok((samples, duration_ns, cpu_utilization_percent))
}

fn measure<F>(
    warmup: Duration,
    minimum_duration: Duration,
    base_rounds: usize,
    minimum_samples: usize,
    mut operation: F,
) -> Result<Measurement, String>
where
    F: FnMut() -> Result<(), String>,
{
    let warmup_start = Instant::now();
    while warmup_start.elapsed() < warmup {
        operation()?;
    }
    if warmup.is_zero() {
        operation()?;
    }
    let mut summaries = Vec::new();
    let mut all_samples = Vec::new();
    let mut elapsed_ns = 0u64;
    let mut target_rounds = base_rounds;
    let mut round_index = 0;
    while round_index < target_rounds {
        let round_start = Instant::now();
        let mut samples = Vec::new();
        while samples.len() < minimum_samples || round_start.elapsed() < minimum_duration {
            let start = Instant::now();
            operation()?;
            samples.push(start.elapsed().as_nanos() as u64);
        }
        let duration_ns = round_start.elapsed().as_nanos() as u64;
        elapsed_ns += duration_ns;
        summaries.push(summarize(&samples, duration_ns));
        all_samples.extend(samples);
        round_index += 1;
        if round_index == base_rounds
            && base_rounds > 1
            && cv(&summaries.iter().map(|r| r.p50_ns).collect::<Vec<_>>()) > 0.05
        {
            target_rounds = 7;
        }
    }
    Ok(Measurement {
        rounds: summaries,
        samples: all_samples,
        elapsed_ns,
    })
}

fn measured(
    server: &str,
    transport: &str,
    workload: &str,
    payload_bytes: Option<usize>,
    concurrency: usize,
    measurement: Measurement,
) -> ResultRow {
    let rate = measurement.samples.len() as f64 / (measurement.elapsed_ns as f64 / 1e9);
    let mean_ns = measurement
        .samples
        .iter()
        .map(|value| *value as f64)
        .sum::<f64>()
        / measurement.samples.len() as f64;
    let cpu_samples = measurement
        .rounds
        .iter()
        .filter_map(|round| round.cpu_utilization_percent)
        .collect::<Vec<_>>();
    let cpu_utilization_percent = if cpu_samples.is_empty() {
        None
    } else {
        Some(cpu_samples.iter().sum::<f64>() / cpu_samples.len() as f64)
    };
    let variation = cv(&measurement
        .rounds
        .iter()
        .map(|round| round.p50_ns)
        .collect::<Vec<_>>());
    let row = ResultRow {
        layer: "controlled",
        client: "rust",
        server: server.to_string(),
        transport: transport.to_string(),
        workload: workload.to_string(),
        concurrency,
        status: if variation > 0.05 {
            "noisy"
        } else {
            "complete"
        }
        .to_string(),
        error: None,
        payload_bytes,
        sample_count: Some(measurement.samples.len()),
        mean_ns: Some(mean_ns),
        p50_ns: Some(percentile(&measurement.samples, 0.50)),
        p95_ns: Some(percentile(&measurement.samples, 0.95)),
        p99_ns: Some(percentile(&measurement.samples, 0.99)),
        operations_per_second: Some(rate),
        payload_bytes_per_second: Some(
            payload_bytes.map_or(0.0, |bytes| rate * bytes as f64 * 2.0),
        ),
        cpu_utilization_percent,
        round_cv: Some(variation),
        rounds: measurement.rounds,
    };
    checkpoint(&row);
    row
}

fn unsupported(
    server: &str,
    transport: &str,
    workload: String,
    payload: Option<usize>,
) -> ResultRow {
    let row = empty_result(server, transport, workload, payload, "unsupported", None);
    checkpoint(&row);
    row
}

fn failed(
    server: &str,
    transport: &str,
    workload: String,
    payload: Option<usize>,
    error: &str,
) -> ResultRow {
    let row = empty_result(
        server,
        transport,
        workload,
        payload,
        "failed",
        Some(error.to_string()),
    );
    checkpoint(&row);
    row
}

fn failed_with_concurrency(
    server: &str,
    transport: &str,
    workload: String,
    payload: Option<usize>,
    concurrency: usize,
    error: &str,
) -> ResultRow {
    let mut row = empty_result(
        server,
        transport,
        workload,
        payload,
        "failed",
        Some(error.to_string()),
    );
    row.concurrency = concurrency;
    checkpoint(&row);
    row
}

fn empty_result(
    server: &str,
    transport: &str,
    workload: String,
    payload: Option<usize>,
    status: &str,
    error: Option<String>,
) -> ResultRow {
    ResultRow {
        layer: "controlled",
        client: "rust",
        server: server.to_string(),
        transport: transport.to_string(),
        workload,
        concurrency: 1,
        status: status.to_string(),
        error,
        payload_bytes: payload,
        sample_count: None,
        mean_ns: None,
        p50_ns: None,
        p95_ns: None,
        p99_ns: None,
        operations_per_second: None,
        payload_bytes_per_second: None,
        cpu_utilization_percent: None,
        round_cv: None,
        rounds: Vec::new(),
    }
}

fn checkpoint(row: &ResultRow) {
    let path = CHECKPOINT_PATH
        .get()
        .unwrap_or_else(|| fail("checkpoint path is not initialized"));
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .unwrap_or_else(|error| fail(&format!("cannot open checkpoint: {error}")));
    serde_json::to_writer(&mut file, row)
        .unwrap_or_else(|error| fail(&format!("cannot serialize checkpoint: {error}")));
    writeln!(file).unwrap_or_else(|error| fail(&format!("cannot write checkpoint: {error}")));

    let completed = PROGRESS_COMPLETED.fetch_add(1, Ordering::Relaxed) + 1;
    let total = PROGRESS_TOTAL.load(Ordering::Relaxed);
    let elapsed = PROGRESS_START
        .get()
        .map_or(0.0, |start| start.elapsed().as_secs_f64());
    let eta_seconds = if completed == 0 {
        0.0
    } else {
        elapsed / completed as f64 * total.saturating_sub(completed) as f64
    };
    let metrics = row.mean_ns.map_or_else(
        || format!("status={}", row.status),
        |mean| {
            format!(
                "mean={:.1}us p50={:.1}us rate={:.1}/s{}",
                mean / 1_000.0,
                row.p50_ns.unwrap_or_default() / 1_000.0,
                row.operations_per_second.unwrap_or_default(),
                row.cpu_utilization_percent
                    .map_or(String::new(), |cpu| format!(" cpu={cpu:.1}%")),
            )
        },
    );
    eprintln!(
        "    [{completed}/{total} eta {:.0}s] {}/{}/c={} {metrics}",
        eta_seconds, row.transport, row.workload, row.concurrency,
    );
}

fn workload_names() -> Vec<(String, Option<usize>)> {
    let mut names = vec![
        ("void_noop".into(), None),
        ("add_floats".into(), None),
        ("echo_string_11b".into(), None),
    ];
    for size in [1usize << 10, 64usize << 10, 1usize << 20, 16usize << 20] {
        names.push((format!("echo_binary_{size}"), Some(size)));
    }
    names
}

fn load_workload_names() -> Vec<(String, Option<usize>)> {
    vec![
        ("add_floats_load".into(), None),
        ("echo_binary_1048576_load".into(), Some(1 << 20)),
    ]
}

fn connect(transport: &str, command: Vec<String>) -> Result<Connection, String> {
    match transport {
        "stdio" => RpcClient::connect(&command)
            .map(|client| Connection::Bytes {
                client: client.protocol_version("2.0.0"),
                _listener: None,
            })
            .map_err(|e| e.to_string()),
        "stdio_shm" => RpcClient::shm_connect(&command, 1usize << 30)
            .map(|client| Connection::Bytes {
                client: client.protocol_version("2.0.0"),
                _listener: None,
            })
            .map_err(|e| e.to_string()),
        "unix" => {
            let socket = format!("/tmp/vgib-{}-{}.sock", std::process::id(), SEED);
            let command = command
                .into_iter()
                .map(|arg| arg.replace("{socket}", &socket))
                .collect();
            let (target, child) = spawn_listener(command, "UNIX:")?;
            let client =
                retry_connect(|| RpcClient::unix_connect(&target).map_err(|e| e.to_string()))?;
            Ok(Connection::Bytes {
                client: client.protocol_version("2.0.0"),
                _listener: Some(child),
            })
        }
        "tcp" => {
            let (target, child) = spawn_listener(command, "TCP:")?;
            let (host, port) = target
                .rsplit_once(':')
                .ok_or_else(|| format!("invalid TCP address: {target}"))?;
            let port = port.parse::<u16>().map_err(|e| e.to_string())?;
            let client =
                retry_connect(|| RpcClient::tcp_connect(host, port).map_err(|e| e.to_string()))?;
            Ok(Connection::Bytes {
                client: client.protocol_version("2.0.0"),
                _listener: Some(child),
            })
        }
        "http_identity" | "http_zstd" => {
            let (port, child) = spawn_listener(command, "PORT:")?;
            let compression = if transport == "http_identity" {
                None
            } else {
                Some(3)
            };
            let client = HttpClient::connect(format!("http://127.0.0.1:{port}"))
                .protocol_version("2.0.0")
                .compression_level(compression)
                .build()
                .map_err(|e| e.to_string())?;
            let caps = retry_connect(|| client.capabilities().map_err(|e| e.to_string()))?;
            if transport == "http_zstd"
                && !caps
                    .supported_encodings
                    .iter()
                    .any(|encoding| encoding.eq_ignore_ascii_case("zstd"))
            {
                return Err("server did not negotiate zstd".to_string());
            }
            Ok(Connection::Http {
                client,
                _listener: child,
            })
        }
        other => Err(format!("unknown transport: {other}")),
    }
}

fn launch_listener(transport: &str, command: Vec<String>) -> Result<(String, ChildGuard), String> {
    match transport {
        "unix" => {
            let sequence = LISTENER_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let socket = format!(
                "/tmp/vgib-load-{}-{SEED}-{sequence}.sock",
                std::process::id()
            );
            let command = command
                .into_iter()
                .map(|arg| arg.replace("{socket}", &socket))
                .collect();
            spawn_listener(command, "UNIX:")
        }
        "tcp" => spawn_listener(command, "TCP:"),
        "http_identity" | "http_zstd" => spawn_listener(command, "PORT:"),
        other => Err(format!("load benchmarks do not support transport: {other}")),
    }
}

fn connect_target(transport: &str, target: &str) -> Result<LoadConnection, String> {
    match transport {
        "unix" => {
            retry_connect(|| RpcClient::unix_connect(target).map_err(|error| error.to_string()))
                .map(|client| LoadConnection::Bytes(client.protocol_version("2.0.0")))
        }
        "tcp" => {
            let (host, port) = target
                .rsplit_once(':')
                .ok_or_else(|| format!("invalid TCP address: {target}"))?;
            let port = port.parse::<u16>().map_err(|error| error.to_string())?;
            retry_connect(|| RpcClient::tcp_connect(host, port).map_err(|error| error.to_string()))
                .map(|client| LoadConnection::Bytes(client.protocol_version("2.0.0")))
        }
        "http_identity" | "http_zstd" => {
            let compression = if transport == "http_identity" {
                None
            } else {
                Some(3)
            };
            let client = HttpClient::connect(format!("http://127.0.0.1:{target}"))
                .protocol_version("2.0.0")
                .compression_level(compression)
                .build()
                .map_err(|error| error.to_string())?;
            let capabilities =
                retry_connect(|| client.capabilities().map_err(|error| error.to_string()))?;
            if transport == "http_zstd"
                && !capabilities
                    .supported_encodings
                    .iter()
                    .any(|encoding| encoding.eq_ignore_ascii_case("zstd"))
            {
                return Err("server did not negotiate zstd".to_string());
            }
            Ok(LoadConnection::Http(Box::new(client)))
        }
        other => Err(format!("load benchmarks do not support transport: {other}")),
    }
}

fn retry_connect<T, F>(mut operation: F) -> Result<T, String>
where
    F: FnMut() -> Result<T, String>,
{
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut last_error = String::from("listener was not ready");
    while Instant::now() < deadline {
        match operation() {
            Ok(value) => return Ok(value),
            Err(error) => last_error = error,
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    Err(last_error)
}

fn spawn_listener(command: Vec<String>, prefix: &str) -> Result<(String, ChildGuard), String> {
    let (program, arguments) = command.split_first().ok_or("empty worker command")?;
    let mut process = Command::new(program);
    process
        .args(arguments)
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    #[cfg(unix)]
    unsafe {
        process.pre_exec(|| {
            if libc::setpgid(0, 0) == -1 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
    let child = process
        .spawn()
        .map_err(|e| format!("spawn {program}: {e}"))?;
    let mut guard = ChildGuard(child);
    let stdout = guard.0.stdout.take().ok_or("worker stdout unavailable")?;
    let mut reader = BufReader::new(stdout);
    let mut line = String::new();
    for _ in 0..20 {
        line.clear();
        if reader.read_line(&mut line).map_err(|e| e.to_string())? == 0 {
            break;
        }
        if let Some(target) = line.trim().strip_prefix(prefix) {
            return Ok((target.to_string(), guard));
        }
    }
    Err(format!("worker did not emit {prefix}<target>"))
}

fn empty_batch() -> RecordBatch {
    RecordBatch::new_empty(Arc::new(Schema::empty()))
}

fn batch_f64(fields: &[(&str, f64)]) -> RecordBatch {
    let schema = Arc::new(Schema::new(
        fields
            .iter()
            .map(|(name, _)| Field::new(*name, DataType::Float64, false))
            .collect::<Vec<_>>(),
    ));
    let columns = fields
        .iter()
        .map(|(_, value)| Arc::new(Float64Array::from(vec![*value])) as Arc<dyn Array>)
        .collect();
    RecordBatch::try_new(schema, columns).unwrap()
}

fn batch_string(name: &str, value: &str) -> RecordBatch {
    let schema = Arc::new(Schema::new(vec![Field::new(name, DataType::Utf8, false)]));
    RecordBatch::try_new(schema, vec![Arc::new(StringArray::from(vec![value]))]).unwrap()
}

fn payload_batch(size: usize) -> RecordBatch {
    let schema = Arc::new(Schema::new(vec![Field::new(
        "value",
        DataType::LargeBinary,
        false,
    )]));
    let data: Vec<u8> = (0..size)
        .map(|index| {
            (index as u64)
                .wrapping_mul(6364136223846793005)
                .wrapping_add(SEED)
                .to_be_bytes()[0]
        })
        .collect();
    RecordBatch::try_new(
        schema,
        vec![Arc::new(LargeBinaryArray::from(vec![Some(
            data.as_slice(),
        )]))],
    )
    .unwrap()
}

fn validate_payload(batch: &RecordBatch, expected: usize) -> Result<(), String> {
    let array = batch
        .column(0)
        .as_any()
        .downcast_ref::<LargeBinaryArray>()
        .ok_or("payload response was not large_binary")?;
    if array.value(0).len() != expected {
        return Err(format!(
            "payload length was {}, expected {expected}",
            array.value(0).len()
        ));
    }
    Ok(())
}

fn validate_unary(workload: &str, batch: &RecordBatch) -> Result<(), String> {
    match workload {
        "void_noop" => Ok(()),
        "add_floats" => {
            let array = batch
                .column(0)
                .as_any()
                .downcast_ref::<Float64Array>()
                .ok_or("add_floats result was not float64")?;
            if (array.value(0) - 3.0).abs() < f64::EPSILON {
                Ok(())
            } else {
                Err("add_floats returned the wrong value".into())
            }
        }
        "echo_string_11b" => {
            let array = batch
                .column(0)
                .as_any()
                .downcast_ref::<StringArray>()
                .ok_or("echo_string result was not utf8")?;
            if array.value(0) == "hello world" {
                Ok(())
            } else {
                Err("echo_string returned the wrong value".into())
            }
        }
        _ => Err(format!("unknown unary workload: {workload}")),
    }
}

fn summarize(samples: &[u64], duration_ns: u64) -> RoundSummary {
    RoundSummary {
        sample_count: samples.len(),
        duration_ns,
        p50_ns: percentile(samples, 0.50),
        p95_ns: percentile(samples, 0.95),
        p99_ns: percentile(samples, 0.99),
        min_ns: *samples.iter().min().unwrap(),
        max_ns: *samples.iter().max().unwrap(),
        cpu_utilization_percent: None,
    }
}

fn cpu_times() -> Option<(u64, u64)> {
    let stat = fs::read_to_string("/proc/stat").ok()?;
    let line = stat.lines().next()?;
    let values = line
        .split_whitespace()
        .skip(1)
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    if values.len() < 4 {
        return None;
    }
    let total = values.iter().sum();
    let idle = values[3] + values.get(4).copied().unwrap_or(0);
    Some((total, idle))
}

fn cpu_utilization(start: (u64, u64), end: (u64, u64)) -> Option<f64> {
    let total = end.0.checked_sub(start.0)?;
    let idle = end.1.checked_sub(start.1)?;
    if total == 0 {
        return None;
    }
    Some((1.0 - idle as f64 / total as f64) * 100.0)
}

fn percentile(samples: &[u64], fraction: f64) -> f64 {
    let mut sorted = samples.to_vec();
    sorted.sort_unstable();
    let position = (sorted.len() - 1) as f64 * fraction;
    let lower = position.floor() as usize;
    let upper = position.ceil() as usize;
    if lower == upper {
        sorted[lower] as f64
    } else {
        sorted[lower] as f64 * (upper as f64 - position)
            + sorted[upper] as f64 * (position - lower as f64)
    }
}

fn cv(values: &[f64]) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mean = values.iter().sum::<f64>() / values.len() as f64;
    if mean == 0.0 {
        return 0.0;
    }
    let variance = values
        .iter()
        .map(|value| (value - mean).powi(2))
        .sum::<f64>()
        / values.len() as f64;
    variance.sqrt() / mean
}

fn shuffled_transports() -> Vec<&'static str> {
    let mut values = TRANSPORTS.to_vec();
    let mut state = SEED;
    for index in (1..values.len()).rev() {
        state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
        values.swap(index, state as usize % (index + 1));
    }
    values
}

fn fail(message: &str) -> ! {
    eprintln!("benchmark driver: {message}");
    std::process::exit(2);
}

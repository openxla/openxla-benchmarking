#!/usr/bin/env python3
#
# Copyright 2023 The OpenXLA Authors
#
# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

import argparse
import dataclasses
import json
import pathlib
import re
import statistics
import subprocess
import sys
from typing import Any, Dict, List, Sequence

# Add common_benchmark_suite dir to the search path.
sys.path.insert(
    0, str(pathlib.Path(__file__).parents[2] / "common_benchmark_suite"))
# Add comparative_benchmark dir to the search path.
sys.path.insert(
    0, str(pathlib.Path(__file__).parents[2] / "comparative_benchmark"))

from openxla.benchmark import def_types
import openxla.benchmark.comparative_suite.jax.benchmark_definitions as jax_benchmark_definitions
import utils

TIME_UNITS = {"us": 1e-3, "ms": 1, "s": 1e3, "min": 60 * 1e3, "h": 3600 * 1e3}
TIME_REGEXP = re.compile(r"time: (\d+\.?\d*) (%s)" % "|".join(TIME_UNITS))
SIZE_REGEXP = re.compile(r" (\d+) bytes")
LOG_TIME_REGEXP = re.compile(
    rb"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}\.\d+):")

GPU_COMPILE_TIME_REGEXP = re.compile(
    rb"NVPTXCompiler::CompileTargetBinary - CompileToPtx.*")
GPU_PEAK_MEMORY_REGEXP = re.compile(
    rb"New Peak memory usage of \d+ bytes for GPU")
GPU_LATENCY_START_REGEXP = re.compile(rb".+HloRunner: ExecuteOnDevices started")
GPU_LATENCY_STOP_REGEXP = re.compile(
    rb".+HloRunner: ExecuteOnDevices succeeded")

CPU_COMPILE_TIME_REGEXP = re.compile(r"... compiled and ran in (.*)s.")
CPU_LATENCY_REGEXP = re.compile(r"execution time for runner [A-Za-z]*: (.*)s.")

HLO_FILENAME = "xla_hlo_before_optimizations.txt"


def _parse_log_time(line: bytes) -> float:
  """Parses timestamp from the standard log."""
  match = LOG_TIME_REGEXP.search(line)
  assert match, "Unable to parse log time: %s" % line
  _, h, m, s = match.groups()
  return 1000 * (int(h) * 3600 + int(m) * 60 + float(s))


def _parse_log_elapsed_time(line1: bytes, line2: bytes) -> float:
  """Calculates elapsed time between two log lines.
  """
  start, end = _parse_log_time(line1), _parse_log_time(line2)
  end += 86400 if end < start else 0  # next day correction
  return end - start


def _parse_latencies(raw_output: bytes,
                     expected_iterations: int) -> List[float]:
  """Returns a list of latencies in milliseconds parsed from XLA logs."""
  start_matches = GPU_LATENCY_START_REGEXP.findall(raw_output)
  stop_matches = GPU_LATENCY_STOP_REGEXP.findall(raw_output)

  if len(start_matches) != len(stop_matches):
    print(
        f"Error: Unequal number of start and stop logs. {len(start_matches)} start logs != {len(stop_matches)} stop logs."
    )
    return []

  if len(start_matches) != expected_iterations:
    print(
        f"Error: Number of iterations not equal to the number of expected iteration. Expected {expected_iterations}. Found {len(start_matches)}."
    )
    return []

  latencies = [
      _parse_log_elapsed_time(t1, t2)
      for t1, t2 in zip(start_matches, stop_matches)
  ]
  return latencies


def _parse_log_duration(time_str: bytes) -> float:
  """Returns the time in milliseconds parsed from XLA logs."""
  match = TIME_REGEXP.search(time_str.decode())
  assert match, "Unable to parse the time on log line"
  exp = TIME_UNITS[match.group(2)]
  return float(match.group(1)) * exp


def _parse_log_size(size_str: bytes) -> float:
  """Returns the size in bytes parsed from XLA logs."""
  match = SIZE_REGEXP.search(size_str.decode())
  assert match, "Unable to parse the size on log line"
  return float(match.group(1)) * 1e-6


def _parse_compile_time(raw_output: bytes) -> float:
  matches = GPU_COMPILE_TIME_REGEXP.findall(raw_output)
  total_compile_time_ms = sum([_parse_log_duration(t1) for t1 in matches])
  return total_compile_time_ms * 1e-3


def _parse_peak_memory(raw_output: bytes) -> float:
  matches = GPU_PEAK_MEMORY_REGEXP.findall(raw_output)
  assert matches, "Unable to find peak memory"
  return _parse_log_size(matches[-1])


def _run_compiler_benchmark_gpu(
    hlo_benchmark_tool_path: pathlib.Path,
    hlo_input_path: pathlib.Path,
    benchmark_iterations: int,
    device: str,
) -> Dict[str, Any]:
  cmd = [
      hlo_benchmark_tool_path,
      f"--hlo_file={hlo_input_path}",
      f"--device_type={device}",
      f"--num_repeats={benchmark_iterations}",
      "--input_format=text",
      "--num_replicas=1",
      "--num_partitions=1",
      "--logtostderr",
  ]

  result = subprocess.run(
      cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      # Timings are logged under VLOG so we need to enable this for the modules
      # we are intereated in.
      env={
          "TF_CPP_MIN_LOG_LEVEL":
              "0",
          "TF_CPP_VMODULE":
              "nvptx_compiler=1,gpu_compiler=1,parse_flags_from_env=1,bfc_allocator=2,functional_hlo_runner=1",
      })
  result_text = result.stdout

  latencies = _parse_latencies(result_text, benchmark_iterations)
  compile_time_s = _parse_compile_time(result_text)
  peak_memory_usage = _parse_peak_memory(result_text)

  results_dict = {
      "compile_time_s": compile_time_s,
      "min_latency_ms": min(latencies, default=None),
      "max_latency_ms": max(latencies, default=None),
      "mean_latency_ms": statistics.mean(latencies) if latencies else None,
      "median_latency_ms": statistics.median(latencies) if latencies else None,
      "stddev_latency_ms": statistics.stdev(latencies) if latencies else None,
      "benchmark_iterations": benchmark_iterations,
      "device_memory_peak_mb": peak_memory_usage,
  }
  return results_dict


def _run_compiler_benchmark_cpu(
    hlo_benchmark_tool_path: pathlib.Path,
    hlo_input_path: pathlib.Path,
    benchmark_iterations: int,
    device: str,
) -> Dict[str, Any]:
  cmd = [
      hlo_benchmark_tool_path,
      "--input_format=hlo",
      f"--platform={device}",
      "--reference_platform=",
      "--logtostderr",
      f"--input_module={hlo_input_path}",
      f"--iterations={benchmark_iterations}",
  ]
  result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
  result_text = result.stdout.decode("utf-8")

  matches = CPU_COMPILE_TIME_REGEXP.findall(result_text)
  # Take the first iteration compile-time latency. Profiles show that this is
  # where tuning and other initialization occurs. Subsequent calls to compile
  # in the same process will reuse these results.
  compile_time_latency = float(matches[0]) if matches else None

  matches = CPU_LATENCY_REGEXP.findall(result_text)
  assert len(matches) == benchmark_iterations, (
      f"Expected to find {benchmark_iterations} latencies but found "
      f"{len(matches)} instead:\n{result_text}")
  latencies = [float(match) * 1000 for match in matches]

  results_dict = {
      "compile_time_s": compile_time_latency,
      "min_latency_ms": min(latencies, default=None),
      "max_latency_ms": max(latencies, default=None),
      "mean_latency_ms": statistics.mean(latencies) if latencies else None,
      "median_latency_ms": statistics.median(latencies) if latencies else None,
      "stddev_latency_ms": statistics.stdev(latencies) if latencies else None,
      "benchmark_iterations": benchmark_iterations,
  }
  return results_dict


def _run(benchmark: def_types.BenchmarkCase, iterations: int,
         hlo_tool: pathlib.Path,
         hlo_dump: pathlib.Path) -> utils.BenchmarkResult:
  model = benchmark.model
  input_data = benchmark.input_data.artifacts[
      def_types.ModelTestDataFormat.NUMPY_TENSORS]
  expected_output = benchmark.expected_output.artifacts[
      def_types.ModelTestDataFormat.NUMPY_TENSORS]

  data_type = model.model_parameters["data_type"]
  batch_size = model.model_parameters["batch_size"]
  input_dims = input_data.data_parameters["tensor_dimensions"]
  output_dims = expected_output.data_parameters["tensor_dimensions"]
  benchmark_definition = {
      "benchmark_id": benchmark.id,
      "benchmark_name": benchmark.name,
      "framework": str(model.model_impl.framework_type),
      "data_type": data_type,
      "batch_size": batch_size,
      "inputs": input_dims,
      "outputs": output_dims,
      "compiler": "xla",
      "device": benchmark.target_device.name,
      "tags": model.model_impl.tags + model.tags,
  }

  # We use different binaries for benchmarking gpu and cpu.
  accelerator = benchmark.target_device.accelerator_type
  if accelerator == "gpu":
    metrics = _run_compiler_benchmark_gpu(hlo_tool, hlo_dump, iterations,
                                          accelerator)
  elif accelerator == "cpu":
    metrics = _run_compiler_benchmark_cpu(hlo_tool, hlo_dump, iterations,
                                          accelerator)
  else:
    raise ValueError(f"Unsupported accelerator: '{accelerator}'.")

  return utils.BenchmarkResult(
      definition=benchmark_definition,
      metrics={
          "compiler_level": metrics,
      },
  )


def _download_artifacts(benchmarks: Sequence[def_types.BenchmarkCase],
                        root_dir: pathlib.Path,
                        verbose: bool = False):
  """Download benchmark artifacts."""

  download_list = []
  for benchmark in benchmarks:
    model_artifact = benchmark.model.artifacts[
        def_types.ModelArtifactType.XLA_HLO_DUMP]
    model_path = root_dir / benchmark.model.name / HLO_FILENAME
    download_list.append((model_artifact.source_url, model_path))

  utils.download_files(download_list, verbose=verbose)


def _parse_arguments() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Run XLA compiler benchmarks.")
  parser.add_argument("-o",
                      "--output",
                      type=pathlib.Path,
                      required=True,
                      help="JSON file path to merge the results.")
  parser.add_argument("-name",
                      "--benchmark_name",
                      required=True,
                      help="The unique id that defines a benchmark.")
  parser.add_argument("-iter",
                      "--iterations",
                      type=int,
                      default=10,
                      help="The number of iterations to benchmark.")
  parser.add_argument("--hlo-tool",
                      "--hlo_tool",
                      required=True,
                      help="The path to `run_hlo_module`.")
  parser.add_argument("--root-dir",
                      "--root_dir",
                      type=pathlib.Path,
                      default=pathlib.Path("/tmp/openxla-benchmark/jax_xla"),
                      help="Root directory stores benchmark artifacts.")
  parser.add_argument("--no-download",
                      "--no_download",
                      action="store_true",
                      help="Don't automatically download benchmark artifacts.")
  parser.add_argument("--verbose",
                      action="store_true",
                      help="Show verbose messages.")

  return parser.parse_args()


def main(
    benchmark_name: str,
    output: pathlib.Path,
    root_dir: pathlib.Path,
    hlo_tool: pathlib.Path,
    iterations: int,
    no_download: bool,
    verbose: bool,
):
  name_pattern = re.compile(f"^{benchmark_name}$")
  all_benchmarks = jax_benchmark_definitions.ALL_BENCHMARKS
  benchmarks = [
      benchmark for benchmark in all_benchmarks
      if name_pattern.match(benchmark.name)
  ]

  if not benchmarks:
    all_benchmark_names = "\n".join(
        benchmark.name for benchmark in all_benchmarks)
    raise ValueError(f'No benchmark matches "{benchmark_name}".'
                     f' Available benchmarks:\n{all_benchmark_names}')

  if not no_download:
    _download_artifacts(benchmarks=benchmarks,
                        root_dir=root_dir,
                        verbose=verbose)

  for benchmark in benchmarks:
    hlo_dump = root_dir / benchmark.model.name / HLO_FILENAME
    if not hlo_dump.exists():
      raise ValueError(f"HLO dump not found: '{hlo_dump}'.")

    result = _run(benchmark=benchmark,
                  iterations=iterations,
                  hlo_tool=hlo_tool,
                  hlo_dump=hlo_dump)
    if verbose:
      print(json.dumps(dataclasses.asdict(result), indent=2))

    utils.append_benchmark_result(output, result)


if __name__ == "__main__":
  main(**vars(_parse_arguments()))
import asyncio
import aiohttp
import csv
import time
import json
import random
import string
from tqdm import tqdm
import argparse
from collections import defaultdict
from typing import List, Dict, Any, DefaultDict, Optional, Union
from jsonpath_ng import parse
from termgraph import termgraph as tg

def generate_random_string(length: int = 10) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def generate_random_value(template: str) -> str:
    if template == "{{random_string}}":
        return generate_random_string()
    elif template == "{{random_int}}":
        return str(random.randint(1, 1000000))
    elif template == "{{random_float}}":
        return f"{random.uniform(0, 1000):.2f}"
    else:
        return template

def generate_json_body(template: Dict[str, Any]) -> Dict[str, Any]:
    return {k: generate_random_value(v) if isinstance(v, str) else v for k, v in template.items()}

def extract_json_values(json_data: Dict[str, Any], json_paths: List[str]) -> Dict[str, Any]:
    extracted_values = {}
    for path in json_paths:
        jsonpath_expr = parse(path)
        matches = [match.value for match in jsonpath_expr.find(json_data)]
        extracted_values[path] = matches[0] if matches else None
    return extracted_values

async def make_request(
    session: aiohttp.ClientSession,
    url: str,
    method: str,
    json_template: Optional[Dict[str, Any]],
    json_paths: List[str],
    semaphore: Optional[asyncio.Semaphore] = None
) -> Dict[str, Any]:
    start_time = time.time()
    try:
        json_body = generate_json_body(json_template) if json_template else None
        if semaphore:
            async with semaphore:
                if method == 'GET':
                    async with session.get(url) as response:
                        elapsed = time.time() - start_time
                        content = await response.text()
                elif method == 'POST':
                    async with session.post(url, json=json_body) as response:
                        elapsed = time.time() - start_time
                        content = await response.text()
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")
        else:
            if method == 'GET':
                async with session.get(url) as response:
                    elapsed = time.time() - start_time
                    content = await response.text()
            elif method == 'POST':
                async with session.post(url, json=json_body) as response:
                    elapsed = time.time() - start_time
                    content = await response.text()
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

        try:
            json_response = json.loads(content)
            extracted_values = extract_json_values(json_response, json_paths)
        except json.JSONDecodeError:
            extracted_values = {path: None for path in json_paths}

        return {
            'url': url,
            'method': method,
            'status': response.status,
            'latency': elapsed,
            'request': str(response.request_info),
            'request_body': json.dumps(json_body) if json_body else '',
            'response': content,
            **extracted_values
        }
    except Exception as e:
        return {
            'url': url,
            'method': method,
            'status': 'Error',
            'latency': time.time() - start_time,
            'request': url,
            'request_body': json.dumps(json_body) if json_body else '',
            'response': str(e),
            **{path: None for path in json_paths}
        }

async def pre_check(url: str, method: str, json_template: Optional[Dict[str, Any]], json_paths: List[str]) -> Dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        result = await make_request(session, url, method, json_template, json_paths)
    return result

async def load_test(
    urls: List[str],
    method: str,
    json_template: Optional[Dict[str, Any]],
    json_paths: List[str],
    rate_limit: int,
    total_requests: int
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    semaphore = asyncio.Semaphore(rate_limit)

    async with aiohttp.ClientSession() as session:
        tasks: List[asyncio.Task] = []
        with tqdm(total=total_requests, desc="Requests", unit="req") as pbar:
            for i in range(total_requests):
                url = urls[i % len(urls)]
                task = asyncio.create_task(make_request(session, url, method, json_template, json_paths, semaphore))
                task.add_done_callback(lambda t: results.append(t.result()))
                task.add_done_callback(lambda _: pbar.update(1))
                tasks.append(task)

                if i < total_requests - 1:
                    await asyncio.sleep(1 / rate_limit)  # Rate limiting

            await asyncio.gather(*tasks)

    return results

def write_report(results: List[Dict[str, Any]], output_file: str, json_paths: List[str]) -> None:
    fieldnames = ['url', 'method', 'status', 'latency', 'request', 'request_body', 'response'] + json_paths
    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(result)

def create_latency_chart(results: List[Dict[str, Any]]) -> None:
    latencies = [result['latency'] for result in results]

    # Create latency ranges
    ranges = [
        (0, 0.1), (0.1, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1),
        (1, 1.5), (1.5, 2), (2, 3), (3, 5), (5, float('inf'))
    ]

    # Count latencies in each range
    counts = [sum(1 for l in latencies if r[0] <= l < r[1]) for r in ranges]

    # Prepare data for termgraph
    data = [
        [f"{r[0]}-{r[1] if r[1] != float('inf') else '5+'}s", count]
        for r, count in zip(ranges, counts) if count > 0
    ]

    # Chart labels
    labels = [item[0] for item in data]
    data = [item[1:] for item in data]

    # Additional args for termgraph
    args = {
        'stacked': False,
        'vertical': False,
        'width': 50,
        'format': '{:<5.0f}',
        'suffix': '',
        'no_labels': False,
        'no_values': False,
        'histogram': ''
    }

    print("\nLatency Distribution:")
    tg.chart(colors=["blue"], data=data, args=args, labels=labels)

def print_summary(results: List[Dict[str, Any]]) -> None:
    status_counts: DefaultDict[Any, int] = defaultdict(int)
    method_counts: DefaultDict[str, int] = defaultdict(int)
    total_latency = 0.0
    max_latency = 0.0
    min_latency = float('inf')

    for result in results:
        status_counts[result['status']] += 1
        method_counts[result['method']] += 1
        latency = result['latency']
        total_latency += latency
        max_latency = max(max_latency, latency)
        min_latency = min(min_latency, latency)

    print("\nSummary:")
    print(f"Total requests: {len(results)}")
    print(f"Average latency: {total_latency / len(results):.2f} seconds")
    print(f"Min latency: {min_latency:.2f} seconds")
    print(f"Max latency: {max_latency:.2f} seconds")
    print("\nMethod distribution:")
    for method, count in method_counts.items():
        print(f"  {method}: {count}")
    print("\nStatus code distribution:")
    for status, count in status_counts.items():
        print(f"  {status}: {count}")

    # Add latency chart
    create_latency_chart(results)

async def main() -> None:
    parser = argparse.ArgumentParser(description="Rate-limited HTTP load testing script")
    parser.add_argument("urls", nargs="+", help="URLs to test (can provide multiple)")
    parser.add_argument("--method", choices=['GET', 'POST'], default='GET', help="HTTP method to use")
    parser.add_argument("--json-template", type=str, help="JSON template for request body")
    parser.add_argument("--json-paths", nargs="+", help="JSON paths to extract from response")
    parser.add_argument("--rate", type=int, default=10, help="Rate limit (requests per second)")
    parser.add_argument("--requests", type=int, default=100, help="Total number of requests to make")
    parser.add_argument("--output", default="load_test_results.csv", help="Output CSV file name")
    args = parser.parse_args()

    json_template = json.loads(args.json_template) if args.json_template else None
    json_paths = args.json_paths if args.json_paths else []

    if args.method == 'POST' and not json_template:
        parser.error("POST method requires a JSON template (use --json-template)")

    # Pre-check step
    print("Performing pre-check...")
    pre_check_result = await pre_check(args.urls[0], args.method, json_template, json_paths)

    if pre_check_result['status'] == 'Error' or pre_check_result['status'] >= 400:
        print(f"Pre-check failed. Error: {pre_check_result['response']}")
        user_input = input("Do you want to continue with the load test? (y/n): ").lower()
        if user_input != 'y':
            print("Exiting the script.")
            return

    print(f"Starting load test with {args.requests} {args.method} requests at {args.rate} requests per second")
    results = await load_test(args.urls, args.method, json_template, json_paths, args.rate, args.requests)

    write_report(results, args.output, json_paths)
    print(f"\nDetailed results written to {args.output}")

    print_summary(results)

if __name__ == "__main__":
    asyncio.run(main())

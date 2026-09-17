"""排序实现的吞吐测量入口（Popper staged_artifacts 试点工程）。

候选只报告自己数出的**原始测量**——基本操作数（比较 + 元素移动）与墙上时间；
「每秒基本操作数」由域包从这两个数派生，候选若在制品里写 ``throughput`` 会被拒绝。

参数由域包声明的调用契约给出：``--warmup``（预热清单，不计时）/ ``--workload``（工作负载）
/ ``--output``（测量制品）/ ``--config``（算法选择）/ ``--seed``（本次独立重复测量的取值）。
"""
import argparse
import json
import random
import time
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Counter:
    """基本操作计数器：一次比较或一次元素移动各记 1。"""

    def __init__(self):
        self.operations = 0

    def less(self, a, b):
        self.operations += 1
        return a < b

    def move(self):
        self.operations += 1


def insertion_sort(values, counter):
    result = list(values)
    for i in range(1, len(result)):
        current = result[i]
        counter.move()
        j = i - 1
        while j >= 0 and counter.less(result[j], current):
            result[j + 1] = result[j]
            counter.move()
            j -= 1
        result[j + 1] = current
        counter.move()
    return result


def merge_sort(values, counter):
    if len(values) <= 1:
        return list(values)
    middle = len(values) // 2
    left = merge_sort(values[:middle], counter)
    right = merge_sort(values[middle:], counter)
    merged = []
    i = j = 0
    while i < len(left) and j < len(right):
        if counter.less(right[j], left[i]):
            merged.append(right[j])
            j += 1
        else:
            merged.append(left[i])
            i += 1
        counter.move()
    for value in left[i:]:
        merged.append(value)
        counter.move()
    for value in right[j:]:
        merged.append(value)
        counter.move()
    return merged


def shell_sort(values, counter):
    result = list(values)
    gap = len(result) // 2
    while gap > 0:
        for i in range(gap, len(result)):
            current = result[i]
            counter.move()
            j = i
            while j >= gap and counter.less(result[j - gap], current):
                result[j] = result[j - gap]
                counter.move()
                j -= gap
            result[j] = current
            counter.move()
        gap //= 2
    return result


ALGORITHMS = {"insertion": insertion_sort, "merge": merge_sort, "shell": shell_sort}


def permutation(size, seed):
    values = list(range(size))
    random.Random(seed).shuffle(values)
    return values


def main():
    parser = argparse.ArgumentParser()
    for name in ("warmup", "workload", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    algorithm = load(args.config)["algorithm"]
    if algorithm not in ALGORITHMS:
        raise ValueError(f"unknown algorithm: {algorithm!r}")
    sort = ALGORITHMS[algorithm]
    seed = int(args.seed)
    # 预热不计时：排除解释器与分配器的冷启动，使不同实现之间的时间可比。
    for row in load(args.warmup):
        sort(permutation(row["n"], seed + 1), Counter())
    counter = Counter()
    started = time.perf_counter()
    for row in load(args.workload):
        sort(permutation(row["n"], seed), counter)
    elapsed = time.perf_counter() - started
    Path(args.output).write_text(
        json.dumps({"operations": counter.operations, "elapsed_seconds": elapsed}),
        encoding="utf-8")


if __name__ == "__main__":
    main()

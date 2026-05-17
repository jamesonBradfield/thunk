from demo_a import compute_doubled


def run_all(values):
    return [compute_doubled(v) for v in values]


if __name__ == "__main__":
    print(run_all([1, 2, 3]))


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b


def power(base, exp):
    return base ** exp


def modulo(a, b):
    return a % b

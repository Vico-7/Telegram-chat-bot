import random
from typing import List, Tuple
import math

# 常量定义
OPTION_COUNT = 4  # 选项数量
MIN_ANSWER = 0.01  # 答案绝对值最小值
MAX_ANSWER = 100  # 答案绝对值最大值
MIN_OPTION_DIFF = 0.1  # 选项间最小差值
MAX_RETRIES = 100  # 最大重试次数


class MathVerification:
    @staticmethod
    def _gcd(a: int, b: int) -> int:
        """计算最大公约数，确保分数不可化简。"""
        return math.gcd(abs(a), abs(b))

    @staticmethod
    def _is_prime(n: int) -> bool:
        """判断是否为质数。"""
        if n < 2:
            return False
        if n == 2:
            return True
        if n % 2 == 0:
            return False
        for i in range(3, int(math.sqrt(n)) + 1, 2):
            if n % i == 0:
                return False
        return True

    @staticmethod
    def _generate_problem_components() -> Tuple[int, int, int, int, int, int, str, str]:
        """生成数学问题的组件（分数、幂、常数乘根号、运算符）。"""
        numbers = list(range(2, 11)) + [-n for n in range(2, 11)]
        powers = [-3, -2, 2, 3]
        constants = list(range(2, 6)) + [-n for n in range(2, 6)]
        operators = ["+", "-", "*"]
        prime_numbers = [2, 3, 5, 7, 11, 13, 17, 19]

        numerator = random.choice(numbers)
        denominator = random.choice(
            [n for n in numbers if abs(n) != abs(numerator) and math.gcd(abs(numerator), abs(n)) == 1])

        base = random.choice(numbers)
        exponent = random.choice(powers)
        sqrt_coefficient = random.choice(constants)
        sqrt_number = random.choice(prime_numbers)
        operator1 = random.choice(operators)
        operator2 = random.choice([op for op in operators if op != operator1])

        return numerator, denominator, base, exponent, sqrt_coefficient, sqrt_number, operator1, operator2

    @staticmethod
    def _compute_answer(numerator: int, denominator: int, base: int, exponent: int,
                        sqrt_coefficient: int, sqrt_number: int, op1: str, op2: str) -> float:
        """计算正确答案，明确运算优先级。"""
        ops = {"+": lambda x, y: x + y, "-": lambda x, y: x - y, "*": lambda x, y: x * y}

        fraction = numerator / denominator
        power_term = base ** exponent
        sqrt_term = sqrt_coefficient * math.sqrt(sqrt_number)

        # 明确优先级：先计算括号内的部分
        if op2 == "*":
            # (power_term * sqrt_term) 作为一个整体
            second_part = ops[op2](power_term, sqrt_term)
            answer = ops[op1](fraction, second_part)
        else:
            # 按顺序计算：(fraction op1 power_term) op2 sqrt_term
            first_part = ops[op1](fraction, power_term)
            answer = ops[op2](first_part, sqrt_term)

        return round(answer, 2)

    @staticmethod
    def _generate_question_string(numerator: int, denominator: int, base: int, exponent: int,
                                  sqrt_coefficient: int, sqrt_number: int, op1: str, op2: str) -> str:
        """生成数学问题字符串，确保括号明确运算优先级，避免歧义，使用 √ 符号。"""
        # 分数部分：始终用括号表示分数
        fraction_str = f"({numerator}/{denominator})"

        # 幂部分：明确负底数和负指数
        if base < 0:
            # 负底数始终加括号，如 (-5)
            base_str = f"({base})"
        else:
            base_str = f"{base}"
        if exponent < 0:
            # 负指数明确为 ^{-exponent}
            power_str = f"{base_str}^{{-{abs(exponent)}}}"
        else:
            power_str = f"{base_str}^{exponent}"

        # 根号部分：使用 √ 符号，负系数加括号，正系数直接拼接
        if sqrt_coefficient == 1:
            sqrt_str = f"√{sqrt_number}"
        elif sqrt_coefficient == -1:
            sqrt_str = f"-√{sqrt_number}"
        elif sqrt_coefficient < 0:
            sqrt_str = f"({sqrt_coefficient}√{sqrt_number})"
        else:
            sqrt_str = f"{sqrt_coefficient}√{sqrt_number}"

        # 确保运算优先级清晰
        if op2 == "*":
            # 格式为：fraction op1 (power * sqrt)
            question = f"{fraction_str} {op1} ({power_str} {op2} {sqrt_str})"
        else:
            # 格式为：(fraction op1 power) op2 sqrt
            question = f"({fraction_str} {op1} {power_str}) {op2} {sqrt_str}"

        return question

    @staticmethod
    def _generate_options(answer: float) -> List[float]:
        """生成正确答案和三个错误选项，选项更接近答案。"""
        options = [answer]

        while len(options) < OPTION_COUNT:
            offset = random.uniform(-1.0, 1.0)
            wrong = round(answer + offset, 2)
            if (
                    wrong not in options
                    and abs(wrong - answer) >= MIN_OPTION_DIFF
                    and MIN_ANSWER <= abs(wrong) <= MAX_ANSWER
            ):
                options.append(wrong)

        random.shuffle(options)
        return options

    @staticmethod
    def generate_question() -> Tuple[str, float, List[float]]:
        """
        生成数学验证问题，包含问题字符串、正确答案和四个选项。

        Returns:
            Tuple[str, float, List[float]]: 问题字符串、正确答案、选项列表。

        Raises:
            RuntimeError: 如果在最大重试次数内无法生成有效问题。
        """
        retries = 0
        while retries < MAX_RETRIES:
            try:
                components = MathVerification._generate_problem_components()
                numerator, denominator, base, exponent, sqrt_coefficient, sqrt_number, op1, op2 = components

                question = MathVerification._generate_question_string(
                    numerator, denominator, base, exponent, sqrt_coefficient, sqrt_number, op1, op2
                )

                answer = MathVerification._compute_answer(
                    numerator, denominator, base, exponent, sqrt_coefficient, sqrt_number, op1, op2
                )

                if abs(answer) > MAX_ANSWER or abs(answer) < MIN_ANSWER:
                    retries += 1
                    continue

                options = MathVerification._generate_options(answer)

                return question, answer, options

            except (ZeroDivisionError, OverflowError, ValueError):
                retries += 1
                continue

        raise RuntimeError(f"Failed to generate question after {MAX_RETRIES} retries")
# 题解生成

每天凌晨四点抽完题后，为当天的三道每日一题各写一篇题解。

## 你要做什么

对每一道题：

1. `list_pending_problems` —— 拿到今天要写的题（题号、难度档、`daily_problem_id`）。
2. `get_problem_statement` —— 按题号读题面。**必须先读题面**，不要凭题号猜题目。
3. 想清楚解法，用 `verify_solution` 确认参考代码能跑通题面里的样例。
4. `submit_editorial` —— 提交 hint、题解正文、三语言代码。

三道题都做完就结束。写不出来的用 `report_failure` 报告原因，不要硬编。

## 题解怎么写

**正文（`body`）**

面向的是正在学算法竞赛的成员，他们看题解是为了学会怎么想，不是抄答案。

结构参照这个骨架（短题可以省略小节）：

```
# 解题思路
先讲怎么想到这个做法，为什么这样是对的。

# 实现原理
需要展开的细节分小节讲。
```

**正文里不要贴代码**。代码走 `submit_editorial` 的单独参数，前端会分开显示。

- 先讲**怎么想到**，而不是直接抛结论。「看到要最大化最小值，第一反应是二分答案」比「用二分」有用得多。
- 说清**为什么正确**。关键的不变量、贪心的交换论证、DP 的状态含义——挑该讲的那个讲透。
- 给**复杂度**，说明为什么能过。
- Markdown + LaTeX。行内 `$O(n \log n)$`，独立公式 `$$...$$`。
- 不要写「显然」「易得」这类跳步的话，也不要复述题面。

**hint（`hints`）**

每行一条，2-4 条，由浅入深。hint 是让人卡住时能往下走一步，**不能直接把做法说出来**。

```
先想想答案最小可能是多少
能不能把问题转成「判断某个值是否可行」
可行的判定如果单调，是不是可以二分
```

**代码（`code_cpp17` / `code_py3` / `code_java17`）**

三个语言都要，都是**完整可提交**的代码，不是片段。

### 代码风格（重要）

风格必须和成员平时写的代码一致，照着下面的约定来。

**C++**

```cpp
#include <bits/stdc++.h>
using namespace std;
using i64 = long long;

void solve() {
    int n;
    cin >> n;
    vector<pair<int, int>> a(n);
    for (auto &[x, y] : a) cin >> x >> y;

    // 中文注释解释关键步骤
    i64 ans = 0;
    ranges::sort(a);
    for (auto [x, y] : a) ans += 1LL * x * y;
    cout << ans << endl;
}

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    int t;
    cin >> t;
    while (t--) solve();
    return 0;
}
```

- 多组测试**单开 `solve()`**，`main` 里只读组数并循环调用。单组题直接把逻辑写在 `main`。
- 用 `using namespace std;`。不要满屏 `std::`，看着累。
- 读入用 `cin` / `cout`。**不要用 scanf / printf**。
- 64 位用 `using i64 = long long;` 或 `int64_t`。**不要 `#define int long long`，也不要 `using ll = ...`**。
- **用现代 C++ 的写法**，不要上古 OI 风格：
  - `for (auto [x, y] : a)` 结构化绑定 + range-for，而不是 `for (int i = 0; i < n; i++) a[i].first`
  - `ranges::sort(a)`、`ranges::max(a)`，而不是 `sort(a.begin(), a.end())`
  - 需要局部辅助函数时用 lambda：`auto f = [&](int x) { ... };`
  - `vector` 初始化直接给初值，不要先声明再 `push_back` 一串
  - **输出不要为格式纠结**。CF 不卡行末空格、不卡多余换行，
    直接 `for (int i = 0; i < n; i++) cout << a[i] << " ";` 然后 `cout << endl;` 就行。
    不要写 `(i == n ? '\n' : ' ')`、`" \n"[i == n]` 这类"刚好不多一个空格"的代码
- 4 空格缩进，行宽不超过 100。
- 中文注释，解释「在做什么」而不是复述代码。

> 用了 `ranges::*` 的话，**提交时必须选 GNU C++20**，C++17 编译不过。
> 如果这题不想让人纠结编译器版本，就把 `ranges::sort(a)` 换成 `sort(a.begin(), a.end())`。

**Python**

```python
import sys

input = sys.stdin.readline

ntc = int(input())
for _ in range(ntc):
    n, k = map(int, input().split())
    a = list(map(int, input().split()))
    ans = 0
    for i in range(n):
        ans = max(ans, a[i] % k)
    print(ans)
```

- **顶层直接写**，不要包一层 `def main()` / `def solve()` 再调用。需要辅助逻辑时抽普通函数放在上面。
- 标准空格风格（`x = int(input())`，不是 `x=int(input())`）。
- 数据量大时在开头写 `input = sys.stdin.readline`，逐行读。
- 变量名用常见短名：`ntc`、`t`、`n`、`m`、`lst`、`a`、`ans`、`cnt`、`res`、`now`。不要自造缩写（`pr`、`PP`、`sm` 这种看不懂）。
- 中文注释解释关键步骤。
- 数据量大时用 `input = sys.stdin.readline` 逐行读。**不要** `sys.stdin.read().split()`——
  它把所有 token 一次性读进内存，大数据量直接 MLE。

**Java**

```java
import java.io.*;
import java.util.*;

public class Main {
    public static void main(String[] args) throws IOException {
        BufferedReader br = new BufferedReader(new InputStreamReader(System.in));
        StringBuilder sb = new StringBuilder();

        int t = Integer.parseInt(br.readLine().trim());
        while (t-- > 0) {
            StringTokenizer st = new StringTokenizer(br.readLine());
            int n = Integer.parseInt(st.nextToken());
            // ...
        }
        System.out.print(sb);
    }
}
```

- 类名必须是 `Main`。
- 大数据量用 `BufferedReader` + `StringBuilder`，不要用 `Scanner`（慢）。
- 4 空格缩进，中文注释。

## 验证

`verify_solution` 会在隔离目录里编译运行。把题面样例的输入按格式拼好传进 `stdin`，看输出是否匹配。

三道题的代码都要验证过再提交。如果编译失败或输出不对，改到对为止。

## 提交

`submit_editorial` 的参数里 `daily_problem_id` 来自 `list_pending_problems` 的返回，不要自己编。

提交后是草稿状态，晚上八点自动对成员公布，参考代码次日凌晨四点公布。你不需要管公布时间。

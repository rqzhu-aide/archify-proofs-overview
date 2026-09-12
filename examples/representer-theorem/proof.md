# The representer theorem: a projection proof

An RKHS may contain infinitely many directions, but a training objective observes a function only at finitely many inputs. The proof separates the directions that determine those observations from the directions that contribute only to the norm penalty.

This is a standalone teaching exposition with local numbering. For background, see Schölkopf, Herbrich, and Smola (2001), [*A Generalized Representer Theorem*](https://alex.smola.org/papers/2001/SchHerSmo01.pdf). It is not a transcription or a complete map of that paper.

## Assumption 1 (RKHS and fixed data)

Let $\mathcal X$ be a nonempty set, and let $\mathcal H$ be a real reproducing kernel Hilbert space of functions on $\mathcal X$, with kernel $k$. Write its inner product and norm as $\langle\cdot,\cdot\rangle_{\mathcal H}$ and $\|\cdot\|_{\mathcal H}$. For every $x\in\mathcal X$, the function $k_x=k(\cdot,x)$ belongs to $\mathcal H$, and

$$
f(x)=\langle f,k_x\rangle_{\mathcal H}
\qquad (f\in\mathcal H).
$$

Fix $n\geq 1$ observations $(x_i,y_i)\in\mathcal X\times\mathbb R$. The inputs need not be distinct. All statements below concern these fixed data; no sampling assumptions are needed.

## Definition 2 (Training span)

Under Assumption 1, define

$$
S=\operatorname{span}\{k_{x_1},\ldots,k_{x_n}\}
\subseteq\mathcal H.
$$

This is a finite dimensional, hence closed, subspace. Let $P_S$ denote the orthogonal projection onto $S$. Membership in $S$ means that a function has an expansion using the training inputs, with at most $n$ coefficients. Those coefficients need not be unique because the kernel sections may be linearly dependent.

## Assumption 3 (Empirical objective and norm penalty)

In the setting of Assumption 1, let
$L:\mathbb R^n\to\mathbb R\cup\{+\infty\}$ be any loss functional, with dependence on the fixed responses absorbed into $L$. Let $\Omega:[0,\infty)\to\mathbb R$ be strictly increasing. Define

$$
J(f)=L\bigl(f(x_1),\ldots,f(x_n)\bigr)
     +\Omega\bigl(\|f\|_{\mathcal H}\bigr),
\qquad f\in\mathcal H.
$$

Thus the loss depends on $f$ only through its training evaluations. The value $+\infty$ can encode constraints on these evaluations. Convexity and continuity are not assumed, and these conditions alone do not assert that a minimum exists.

## Lemma 4 (Projection preserves the training evaluations)

Under Assumption 1 and Definition 2, every $f\in\mathcal H$ has a unique decomposition

$$
f=s+h,\qquad s=P_Sf\in S,\qquad h\in S^\perp.
$$

Moreover,

$$
f(x_i)=s(x_i)\quad (i=1,\ldots,n),
\qquad
\|f\|_{\mathcal H}^2
=\|s\|_{\mathcal H}^2+\|h\|_{\mathcal H}^2.
$$

**Proof.** The orthogonal projection theorem applies because $S$ is closed. For each training input, the reproducing property and $k_{x_i}\in S$ give

$$
h(x_i)=\langle h,k_{x_i}\rangle_{\mathcal H}=0.
$$

Consequently $f(x_i)=s(x_i)$. Expanding the squared norm of $s+h$ and using $\langle s,h\rangle_{\mathcal H}=0$ gives the norm identity. $\square$

## Theorem 5 (Representer theorem)

Under Assumptions 1 and 3, suppose $J$ attains a finite minimum on $\mathcal H$. Every minimizing function $f_*$ belongs to the training span in Definition 2. Equivalently, there exist coefficients $\alpha_1,\ldots,\alpha_n\in\mathbb R$ such that

$$
f_*(\cdot)=\sum_{i=1}^n\alpha_i k(\cdot,x_i).
$$

**Proof.** Apply Lemma 4 to write $f_*=s+h$. Its evaluation identity makes the loss terms for $f_*$ and $s$ equal. Because $J(f_*)$ is finite and $\Omega$ is real valued, this common loss is finite. If $h\neq 0$, the norm identity implies $\|s\|_{\mathcal H}<\|f_*\|_{\mathcal H}$. Strict increase of $\Omega$ then gives

$$
J(s)<J(f_*),
$$

contradicting minimality. Therefore $h=0$, so $f_*\in S$, which is precisely the claimed expansion. $\square$

**Remark.** If $\Omega$ is only nondecreasing, Lemma 4 still gives $J(P_Sf)\leq J(f)$. Whenever a finite minimum is attained, projecting a minimizer therefore produces a minimizer in $S$. It need not follow that every minimizer lies in $S$: if $L\equiv 0$ and $\Omega\equiv 0$, every function minimizes $J$, including functions outside $S$ when that subspace is proper. Neither version asserts existence or uniqueness in general.

## Corollary 6 (Kernel ridge regression)

Under Assumption 1, fix $\lambda>0$ and consider the sum of squared errors objective

$$
J_\lambda(f)=\sum_{i=1}^n\bigl(f(x_i)-y_i\bigr)^2
             +\lambda\|f\|_{\mathcal H}^2.
$$

Let $K\in\mathbb R^{n\times n}$ have entries $K_{ij}=k(x_i,x_j)$, let $y=(y_1,\ldots,y_n)^\top$, and let $I_n$ be the identity matrix. There is a unique minimizing function, given by

$$
f_*(\cdot)=\sum_{i=1}^n\alpha_i k(\cdot,x_i),
\qquad
\alpha=(K+\lambda I_n)^{-1}y.
$$

**Proof.** First establish existence. On the finite dimensional space $S$ of Definition 2, the function $J_\lambda$ is continuous: the reproducing property makes each evaluation a continuous linear functional. Moreover, $J_\lambda(s)\geq\lambda\|s\|_{\mathcal H}^2$. Thus the sublevel set

$$
\{s\in S:J_\lambda(s)\leq J_\lambda(0)\}
$$

is nonempty, closed, and bounded, hence compact. A minimum on this set is a finite minimum over $S$. By Lemma 4, $J_\lambda(P_Sf)\leq J_\lambda(f)$ for every $f\in\mathcal H$, so this minimum is also global.

Assumption 3 holds with squared error loss and $\Omega(t)=\lambda t^2$. We can now apply Theorem 5: every global minimizer belongs to $S$. It remains to solve the coefficient problem and identify which coefficient vectors represent the same function.

The reproducing property makes $K$ symmetric, and for every $v\in\mathbb R^n$,

$$
v^\top Kv
=\left\|\sum_{i=1}^n v_i k_{x_i}\right\|_{\mathcal H}^2\geq 0.
$$

Thus $v^\top(K+\lambda I_n)v\geq\lambda\|v\|_2^2$, so $K+\lambda I_n$ is invertible. For $f_a=\sum_i a_i k_{x_i}$, the objective becomes

$$
q(a):=J_\lambda(f_a)=\|Ka-y\|_2^2+\lambda a^\top Ka.
$$

Set $\alpha=(K+\lambda I_n)^{-1}y$. Since $K\alpha-y=-\lambda\alpha$, expansion of the quadratic terms yields, for every $v\in\mathbb R^n$,

$$
\begin{aligned}
q(\alpha+v)-q(\alpha)
&=2v^\top K\bigl((K+\lambda I_n)\alpha-y\bigr)
  +v^\top K(K+\lambda I_n)v\\
&=\|Kv\|_2^2+\lambda v^\top Kv\geq 0.
\end{aligned}
$$

Equality holds exactly when $Kv=0$. Hence $\alpha$ minimizes $q$, and all minimizing coefficient vectors have the form $\alpha+v$ with $v\in\ker K$. The norm identity above then gives $\sum_i v_i k_{x_i}=0$. Since Theorem 5 confines every global minimizer to $S$, all global minimizers are the same function $f_\alpha$.

The displayed linear system selects a unique coefficient vector, while the minimizing function has other coefficient representations whenever $K$ is singular. $\square$

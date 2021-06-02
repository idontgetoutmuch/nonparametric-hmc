"""Contains naive samplers and HMC samplers."""

import math
import pickle
import time
from typing import Callable, Iterator, List, Tuple

import torch
from tqdm import tqdm

from ppl import ProbRun, T

torch.manual_seed(0)  # makes executions deterministic
torch.set_printoptions(precision=10)  # more precise printing for debugging


class State:
    """Describes a state in phase space (position q, momentum p) for NP-DHMC

    The field `is_cont` stores which variables are continuous.
    """

    def __init__(
        self,
        q: torch.Tensor,
        p: torch.Tensor,
        is_cont: torch.Tensor,
    ) -> None:
        self.q = q
        """position"""
        self.p = p
        """momentum"""
        self.is_cont = is_cont
        """is_cont[i] == True if the density function is continuous in coordinate i.
        
        If a branch (if-statement) in a program depends on self.q[i], it is discontinuous and is_cont[i] == False."""

    def kinetic_energy(self) -> torch.Tensor:
        """Computes the kinetic energy of the particle.

        In discontinuous HMC, discontinuous coordinates use Laplace momentum, not Gaussian momentum."""
        gaussian = self.p * self.is_cont
        laplace = self.p * ~self.is_cont
        return gaussian.dot(gaussian) / 2 + torch.sum(torch.abs(laplace))


def importance_sample(
    run_prog: Callable[[torch.Tensor], ProbRun[T]],
    count: int = 10_000,
) -> Iterator[Tuple[float, T]]:
    """Samples from a probabilistic program using importance sampling.

    The resulting samples are weighted.

    Note: This is not needed to reproduce the results, but hopefully makes the code easier to understand.

    Args:
        run_prog (Callable[[torch.Tensor], ProbRun[T]]): runs the probabilistic program on a trace.
        count (int, optional): the desired number of samples. Defaults to 10_000.

    Yields:
        Iterator[Tuple[torch.Tensor, T]]: samples of the form (log_score, value)
    """
    for _ in tqdm(range(count)):
        result = run_prog(torch.tensor([]))
        yield result.log_score.item(), result.value


def importance_resample(
    run_prog: Callable[[torch.Tensor], ProbRun[T]],
    count: int = 10_000,
) -> Tuple[List[Tuple[float, T]], List[T]]:
    """Samples from a probabilistic program using importance sampling and systematic resampling.

    It uses systematic resampling on the weighted importance samples to obtain unweighted samples.

    Note: This is not needed to reproduce the results, but hopefully makes the code easier to understand.

    Args:
        run_prog (Callable[[torch.Tensor], ProbRun[T]]): runs the probabilistic program on a trace.
        count (int, optional): the desired number of samples. Defaults to 10_000.

    Returns:
        Tuple[List[Tuple[float, T]], List[T]]: weighted samples, resamples
    """
    weighted_samples = list(importance_sample(run_prog, count))
    count = len(weighted_samples)
    mx = max(log_weight for (log_weight, _) in weighted_samples)
    weight_sum = sum(math.exp(log_weight - mx) for (log_weight, _) in weighted_samples)
    # systematic resampling:
    u_n = torch.distributions.Uniform(0, 1).sample().item()
    sum_acc = 0.0
    resamples: List[T] = []
    for (log_weight, value) in weighted_samples:
        weight = math.exp(log_weight - mx) * count / weight_sum
        sum_acc += weight
        while u_n < sum_acc:
            u_n += 1
            resamples.append(value)
    return weighted_samples, resamples


def coord_integrator(
    run_prog: Callable[[torch.Tensor], ProbRun[T]],
    i: int,
    eps: float,
    state: State,
    state_0: State,
    result: ProbRun,
) -> ProbRun[T]:
    """Coordinate integrator adapted from discontinuous HMC.

    For NP-DHMC, it also has to deal with possible changes in dimension."""
    U = -result.log_weight
    q = state.q.clone().detach()
    q[i] += eps * torch.sign(state.p[i])
    new_result = run_prog(q)
    new_U = -new_result.log_weight.item()
    delta_U = new_U - U
    if not math.isfinite(new_U) or torch.abs(state.p[i]) <= delta_U:
        state.p[i] = -state.p[i]
    else:
        state.p[i] -= torch.sign(state.p[i]) * delta_U
        N2 = new_result.len
        N = result.len
        result = new_result
        if N2 > N:
            # extend everything to the higher dimension
            state.q = result.samples.clone().detach()
            is_cont = result.is_cont.clone().detach()
            # pad the momentum vector:
            gauss = torch.distributions.Normal(0, 1).sample([N2 - N])
            laplace = torch.distributions.Laplace(0, 1).sample([N2 - N])
            p_padding = gauss * is_cont[N:N2] + laplace * ~is_cont[N:N2]
            state_0.p = torch.cat((state_0.p, p_padding))
            state_0.is_cont = torch.cat((state_0.is_cont, is_cont[N:N2]))
            state.p = torch.cat((state.p, p_padding))
            state.is_cont = is_cont
        else:
            # truncate everything to the lower dimension
            state.q = result.samples[:N2].clone().detach()
            state.p = state.p[:N2]
            state.is_cont = result.is_cont[:N2]
            state_0.p = state_0.p[:N2]
            state_0.is_cont = state_0.is_cont[:N2]
        assert len(state.p) == len(state_0.p)
        assert len(state.p) == len(state.q)
        assert len(state.is_cont) == len(state.p)
        assert len(state_0.is_cont) == len(state_0.p)
    return result


def integrator_step(
    run_prog: Callable[[torch.Tensor], ProbRun[T]],
    eps: float,
    state: State,
    state_0: State,
) -> ProbRun[T]:
    """Performs one integrator step (called "leapfrog step" in standard HMC)."""
    result = run_prog(state.q)
    # first half of leapfrog step for continuous variables:
    state.p = state.p - eps / 2 * result.gradU() * state.is_cont
    state.q = state.q + eps / 2 * state.p * state.is_cont
    result = run_prog(state.q)
    # Integrate the discontinuous coordinates in a random order:
    disc_indices = torch.flatten(torch.nonzero(~state.is_cont, as_tuple=False))
    perm = torch.randperm(len(disc_indices))
    disc_indices_permuted = disc_indices[perm]
    for j in disc_indices_permuted:
        if j >= len(state.q):
            continue  # out-of-bounds can happen if q changes length during the loop
        result = coord_integrator(run_prog, int(j.item()), eps, state, state_0, result)
    # second half of leapfrog step for continuous variables
    state.q = state.q + eps / 2 * state.p * state.is_cont
    result = run_prog(state.q)
    state.p = state.p - eps / 2 * result.gradU() * state.is_cont
    return result


# Nonparametric discontinuous Hamiltonian Monte Carlo (NP-DHMC)
def np_dhmc(
    run_prog: Callable[[torch.Tensor], ProbRun[T]],
    count: int,
    leapfrog_steps: int,
    eps: float,
    burnin: int = None,
) -> List[T]:
    """Samples from a probabilistic program using NP-DHMC.

    Args:
        run_prog (Callable[[torch.Tensor], ProbRun[T]]): runs the probabilistic program on a trace.
        count (int, optional): the desired number of samples. Defaults to 10_000.
        burnin (int): number of samples to discard at the start. Defaults to `count // 10`.
        leapfrog_steps (int): number of leapfrog steps the integrator performs.
        eps (float): the step size of the leapfrog steps.

    Returns:
        List[T]: list of samples
    """
    if burnin is None:
        burnin = count // 10
    final_samples = []
    result = run_prog(torch.tensor([]))
    U = -result.log_weight
    q = result.samples.clone().detach()
    is_cont = result.is_cont.clone().detach()
    count += burnin
    accept_count = 0
    for _ in tqdm(range(count)):
        N = len(q)
        dt = ((torch.rand(()) + 0.5) * eps).item()
        gaussian = torch.distributions.Normal(0, 1).sample([N]) * is_cont
        laplace = torch.distributions.Laplace(0, 1).sample([N]) * ~is_cont
        p = gaussian + laplace
        state_0 = State(q, p, is_cont)
        state = State(q, p, is_cont)
        prev_res = result
        for _ in range(leapfrog_steps):
            if not math.isfinite(result.log_weight.item()):
                break
            result = integrator_step(run_prog, dt, state, state_0)
        K_0 = state_0.kinetic_energy()
        U_0 = -prev_res.log_weight
        K = state.kinetic_energy()
        U = -result.log_weight
        accept_prob = torch.exp(U_0 + K_0 - U - K)
        if U.item() != math.inf and torch.rand(()) < accept_prob:
            q = state.q
            is_cont = state.is_cont
            accept_count += 1
            final_samples.append(result.value)
        else:
            result = prev_res
            final_samples.append(prev_res.value)
    count = len(final_samples)
    final_samples = final_samples[burnin:]  # discard first samples (burn-in)
    print(f"acceptance ratio: {accept_count / count * 100}%")
    return final_samples


def run_inference(
    run_prog: Callable[[torch.Tensor], ProbRun[T]],
    name: str,
    count: int,
    eps: float,
    leapfrog_steps: int,
    burnin: int = None,
    seed: int = None,
    **kwargs,
) -> dict:
    """Runs importance sampling and NP-DHMC, then saves the samples to a .pickle file.

    The file is located in the `samples_produced/` folder.

    Note: This is not needed to reproduce the results, but hopefully makes the code easier to understand.
    """

    def run(sampler: Callable) -> dict:
        if seed is not None:
            torch.manual_seed(seed)
        start = time.time()
        results = sampler()
        stop = time.time()
        elapsed = stop - start
        return {
            "time": elapsed,
            "samples": results,
        }

    adjusted_count = count * leapfrog_steps
    samples = {}
    print("Running NP-DHMC...")
    samples["hmc"] = run(
        lambda: np_dhmc(
            run_prog,
            count=count,
            eps=eps,
            leapfrog_steps=leapfrog_steps,
            burnin=burnin,
            **kwargs,
        ),
    )
    samples["hmc"]["burnin"] = burnin
    samples["hmc"]["eps"] = eps
    samples["hmc"]["leapfrog_steps"] = leapfrog_steps
    print("Running importance sampling...")
    samples["is"] = run(
        lambda: importance_resample(run_prog, count=adjusted_count),
    )
    weighted, values = samples["is"]["samples"]
    samples["is"]["samples"] = values
    samples["is"]["weighted"] = weighted

    filename = f"{name}__count{count}_eps{eps}_leapfrogsteps{leapfrog_steps}"
    samples["filename"] = filename
    with open(f"samples_produced/{filename}.pickle", "wb") as f:
        pickle.dump(samples, f)
    return samples

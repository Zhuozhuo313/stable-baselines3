import sys
import time

import torch as th
import torch.nn.functional as F
import numpy as np

from torchy_baselines import TD3
from torchy_baselines.common.evaluation import evaluate_policy
from torchy_baselines.cem_rl.cem import CEM


class CEMRL(TD3):
    """
    Implementation of CEM-RL

    Paper: https://arxiv.org/abs/1810.01222
    Code: https://github.com/apourchot/CEM-RL
    """

    def __init__(self, policy, env, policy_kwargs=None, verbose=0,
                 sigma_init=1e-3, pop_size=10, damp=1e-3, damp_limit=1e-5,
                 elitism=False, n_grad=5, policy_freq=2, batch_size=100,
                 buffer_size=int(1e6), learning_rate=1e-3, seed=0, device='cpu',
                 action_noise_std=0.0, start_timesteps=100, _init_setup_model=True):

        super(CEMRL, self).__init__(policy, env, policy_kwargs, verbose,
                                    buffer_size, learning_rate, seed, device,
                                    action_noise_std, start_timesteps,
                                    policy_freq=policy_freq, batch_size=batch_size,
                                    _init_setup_model=False)

        self.es = None
        self.sigma_init = sigma_init
        self.pop_size = pop_size
        self.damp = damp
        self.damp_limit = damp_limit
        self.elitism = elitism
        self.n_grad = n_grad
        self.es_params = None
        self.fitnesses = []

        if _init_setup_model:
            self._setup_model()

    def _setup_model(self, seed=None):
        super(CEMRL, self)._setup_model()
        params_vector = self.actor.parameters_to_vector()
        self.es = CEM(len(params_vector), mu_init=params_vector,
                      sigma_init=self.sigma_init, damp=self.damp, damp_limit=self.damp_limit,
                      pop_size=self.pop_size, antithetic=not self.pop_size % 2, parents=self.pop_size // 2,
                      elitism=self.elitism)

    def learn(self, total_timesteps, callback=None, log_interval=100,
              eval_freq=-1, n_eval_episodes=5, tb_log_name="CEMRL", reset_num_timesteps=True):

        timesteps_since_eval = 0
        actor_steps = 0
        episode_num = 0
        evaluations = []
        start_time = time.time()

        while self.num_timesteps < total_timesteps:

            self.fitnesses = []
            self.es_params = self.es.ask(self.pop_size)

            if callback is not None:
                # Only stop training if return value is False, not when it is None.
                if callback(locals(), globals()) is False:
                    break

            if self.num_timesteps > 0:
                # self.train(episode_timesteps)
                # Gradient steps for half of the population
                for i in range(self.n_grad):
                    # set params
                    self.actor.load_from_vector(self.es_params[i])
                    self.actor_target.load_from_vector(self.es_params[i])
                    self.actor.optimizer = th.optim.Adam(self.actor.parameters(), lr=self.learning_rate)

                    # In the paper: 2 * actor_steps // self.n_grad
                    # From the original implementation:
                    # Difference: the target critic is updated in the train_critic()
                    # instead of the train_actor()
                    # Issue: the bigger the population, the slower the code
                    # self.train_critic(actor_steps // self.n_grad)
                    # self.train_actor(actor_steps)

                    # Closer to td3: policy delay and it scales
                    # with a bigger population
                    for it in range(2 * (actor_steps // self.n_grad)):
                        # Sample replay buffer
                        replay_data = self.replay_buffer.sample(self.batch_size)
                        self.train_critic(replay_data=replay_data)

                        # Delayed policy updates
                        if it % self.policy_freq == 0:
                            self.train_actor(replay_data=replay_data)

                    # Get the params back in the population
                    self.es_params[i] = self.actor.parameters_to_vector()

            # Evaluate episode
            if 0 < eval_freq <= timesteps_since_eval:
                timesteps_since_eval %= eval_freq

                self.actor.load_from_vector(self.es.mu)

                mean_reward, _ = evaluate_policy(self, self.env, n_eval_episodes)
                evaluations.append(mean_reward)

                if self.verbose > 0:
                    print("Eval num_timesteps={}, mean_reward={:.2f}".format(self.num_timesteps, evaluations[-1]))
                    print("FPS: {:.2f}".format(self.num_timesteps / (time.time() - start_time)))
                    sys.stdout.flush()

            actor_steps = 0
            # evaluate all actors
            for params in self.es_params:

                self.actor.load_from_vector(params)

                # Reset environment
                obs = self.env.reset()
                episode_reward = 0
                episode_timesteps = 0
                episode_num += 1
                done = False

                while not done:
                    # Select action randomly or according to policy
                    if self.num_timesteps < self.start_timesteps:
                        action = self.env.action_space.sample()
                    else:
                        action = self.select_action(np.array(obs))

                    if self.action_noise_std > 0:
                        # NOTE: in the original implementation, the noise is applied to the unscaled action
                        action_noise = np.random.normal(0, self.action_noise_std, size=self.action_space.shape[0])
                        action = (action + action_noise).clip(-1, 1)

                    # Rescale and perform action
                    new_obs, reward, done, _ = self.env.step(self.max_action * action)

                    if hasattr(self.env, '_max_episode_steps'):
                        done_bool = 0 if episode_timesteps + 1 == self.env._max_episode_steps else float(done)
                    else:
                        done_bool = float(done)

                    episode_reward += reward

                    # Store data in replay buffer
                    # self.replay_buffer.add(state, next_state, action, reward, done)
                    self.replay_buffer.add(obs, new_obs, action, reward, done_bool)

                    obs = new_obs
                    episode_timesteps += 1
                    # Note: if put on the outer, it will explore start_timesteps for each actor
                    self.num_timesteps += 1

                if self.verbose > 1:
                    print("Total T: {} Episode Num: {} Episode T: {} Reward: {}".format(
                        self.num_timesteps, episode_num, episode_timesteps, episode_reward))

                actor_steps += episode_timesteps
                self.fitnesses.append(episode_reward)

            self.es.tell(self.es_params, self.fitnesses)

            # self.num_timesteps += actor_steps
            timesteps_since_eval += actor_steps
        return self

    def save(self, path):
        if not path.endswith('.pth'):
            path += '.pth'
        th.save(self.policy.state_dict(), path)

    def load(self, path, env=None, **_kwargs):
        if not path.endswith('.pth'):
            path += '.pth'
        if env is not None:
            pass
        self.policy.load_state_dict(th.load(path))
        self._create_aliases()

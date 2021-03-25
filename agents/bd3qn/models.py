import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
from noisy_net import NoisyLinear, NoisyFactorizedLinear
from OneHotEncode import OneHotEncode

class BranchingQNetwork(nn.Module):
    def __init__(self, observation_space, action_space, action_bins, hidden_dim, exploration_method="Epsilon"):
        super().__init__()
        self.exploration_method = exploration_method
        if self.exploration_method == "Noisy":
            self.model = nn.Sequential(
                NoisyLinear(observation_space, hidden_dim*4),
                nn.ReLU(),
                NoisyLinear(hidden_dim*4, hidden_dim*2),
                nn.ReLU(),
                NoisyLinear(hidden_dim*2, hidden_dim),
                nn.ReLU()
            )
            self.value_head = NoisyLinear(hidden_dim, 1)
            self.adv_heads = nn.ModuleList(
                [NoisyLinear(hidden_dim, action_bins) for i in range(action_space)])
        else:
            self.model = nn.ModuleList([nn.Sequential(
                nn.Linear(observation_space, hidden_dim*4),
                nn.ReLU(),
                nn.Linear(hidden_dim*4, hidden_dim*2),
                nn.ReLU(),
                nn.Linear(hidden_dim*2, hidden_dim),
                nn.ReLU()
            ) for i in range(12)])
            self.value_head = nn.Linear(hidden_dim, 1)
            self.adv_heads = nn.ModuleList(
                [nn.Linear(hidden_dim, 11) for i in range(action_space)])

    def forward(self, x):
        x = OneHotEncode(x)
        out = self.model(x)
        value = self.value_head(out)
        if value.shape[0] == 1:
            advs = torch.stack([l(out) for l in self.adv_heads], dim=0)
            q_val = value + advs - advs.mean(1, keepdim=True)
        else:
            advs = torch.stack([l(out) for l in self.adv_heads], dim=1)
            q_val = value.unsqueeze(1) + advs - advs.mean(2, keepdim=True)
        return q_val

    def sample_noise(self):
        self.model[0].sample_noise()
        self.model[2].sample_noise()
        self.model[4].sample_noise()
        self.value_head.sample_noise()
        for l in self.adv_heads:
            l.sample_noise()


class BranchingDQN(nn.Module):
    def __init__(self, observation_space, action_space, action_bins, target_update_freq, learning_rate, gamma, hidden_dim, td_target, device, exploration_method):
        super().__init__()

        self.observation_space = observation_space
        self.action_space = action_space
        self.action_bins = action_bins
        self.gamma = gamma
        self.exploration_method = exploration_method

        self.policy_network = BranchingQNetwork(
            observation_space, action_space, action_bins, hidden_dim, exploration_method)
        self.target_network = BranchingQNetwork(
            observation_space, action_space, action_bins, hidden_dim, exploration_method)
        self.target_network.load_state_dict(self.policy_network.state_dict())

        self.optim = optim.Adam(
            self.policy_network.parameters(), lr=learning_rate)

        self.policy_network.to(device)
        self.target_network.to(device)
        self.device = device

        self.target_update_freq = target_update_freq
        self.update_counter = 0

        self.td_target = td_target

    def get_action(self, x):
        turn = x[0]
        x = torch.from_numpy(x).float()
        x = x.to(self.device)
        with torch.no_grad():
            if self.exploration_method == "Noisy":
                self.policy_network.sample_noise()
            out = self.policy_network(x).squeeze(0)
            action = torch.argmax(out, dim=1)

        #print("Turn {}:".format(turn), out.max(1, keepdim = True))
        return action.detach().cpu().numpy()  # action.numpy()

    def update_policy(self, batch, memory):
        sample, batch_indxs, batch_weights = batch
        
        batch_states = sample[0]
        batch_actions = sample[1]
        batch_rewards = sample[2]
        batch_next_states = sample[3]
        batch_done = sample[4]

        states = torch.tensor(batch_states).float().to(self.device)
        actions = torch.tensor(batch_actions).long().reshape(
            states.shape[0], -1, 1).to(self.device)
        rewards = torch.tensor(batch_rewards).float(
        ).reshape(-1, 1).to(self.device)
        next_states = torch.tensor(batch_next_states).float().to(self.device)
        if self.exploration_method == "Noisy":
            self.policy_network.sample_noise()
        current_Q = self.policy_network(states).gather(2, actions).squeeze(-1)
        if self.td_target == "mean":
            current_Q = current_Q.mean(1, keepdim=True)
        elif self.td_target == "max":
            current_Q, _ = current_Q.max(1, keepdim=True)
        with torch.no_grad():
            argmax = torch.argmax(self.policy_network(next_states), dim=2)
            if self.exploration_method == "Noisy":
                self.target_network.sample_noise()
            max_next_Q = self.target_network(next_states).gather(
                2, argmax.unsqueeze(2)).squeeze(-1)
            if self.td_target == "mean":
                max_next_Q = max_next_Q.mean(1, keepdim=True)
            elif self.td_target == "max":
                max_next_Q, _ = max_next_Q.max(1, keepdim=True)

        #print("Current Q", current_Q)
        expected_Q = rewards + max_next_Q * self.gamma
        errors = torch.abs(expected_Q - current_Q).cpu().data.numpy()
        #print("Expect:", expected_Q, "Current:", current_Q, "Error:", errors)
        #print("Expect Q", expected_Q)
        batch_weights = torch.from_numpy(batch_weights).float()
        batch_weights = batch_weights.to(self.device)
        loss = (batch_weights *
                F.mse_loss(current_Q, expected_Q)).mean()
        # print(self.policy_network)
        self.optim.zero_grad()
        loss.backward()
        # print(self.policy_network.parameters())
        for p in self.policy_network.parameters():
            p.grad.data.clamp_(-1., 1.)
        self.optim.step()
        memory.update_priorities(batch_indxs, errors)

        self.update_counter += 1
        if self.update_counter % self.target_update_freq == 0:
            #print("Update target net")
            self.update_counter = 0
            self.target_network.load_state_dict(
                self.policy_network.state_dict())

        return loss.detach().cpu()

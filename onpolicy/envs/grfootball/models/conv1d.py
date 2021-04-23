import time
import pprint
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import Categorical

class Model(nn.Module):
    def __init__(self, arg_dict, device=None):
        super(Model, self).__init__()
        self.device=None
        if device:
            self.device = device

        self.arg_dict = arg_dict

        self.fc_player = nn.Linear(arg_dict["feature_dims"]["player"],64)  
        self.fc_ball = nn.Linear(arg_dict["feature_dims"]["ball"],64)
        self.fc_left = nn.Linear(arg_dict["feature_dims"]["left_team"],48)
        self.fc_right  = nn.Linear(arg_dict["feature_dims"]["right_team"],48)
        self.fc_left_closest = nn.Linear(arg_dict["feature_dims"]["left_team_closest"],48)
        self.fc_right_closest = nn.Linear(arg_dict["feature_dims"]["right_team_closest"],48)
        
        self.conv1d_left = nn.Conv1d(48, 36, 1, stride=1)
        self.conv1d_right = nn.Conv1d(48, 36, 1, stride=1)
        # self.fc_left2 = nn.Linear(36*10,96)
        # self.fc_right2 = nn.Linear(36*11,96)
        # self.fc_cat = nn.Linear(96+96+64+64+48+48,arg_dict["lstm_size"])
        ###rjq 上面三行更改为
        self.fc_left2 = nn.Linear(36*2,96)
        self.fc_right2 = nn.Linear(36*2,96)
        self.fc_cat = nn.Linear(96+96+64+64+48+48,arg_dict["lstm_size"])
        
        self.norm_player = nn.LayerNorm(64)
        self.norm_ball = nn.LayerNorm(64)
        self.norm_left = nn.LayerNorm(48)
        self.norm_left2 = nn.LayerNorm(96)
        self.norm_left_closest = nn.LayerNorm(48)
        self.norm_right = nn.LayerNorm(48)
        self.norm_right2 = nn.LayerNorm(96)
        self.norm_right_closest = nn.LayerNorm(48)
        self.norm_cat = nn.LayerNorm(arg_dict["lstm_size"])
        
        self.lstm  = nn.LSTM(arg_dict["lstm_size"], arg_dict["lstm_size"])  #(256,256)

        self.fc_pi_a1 = nn.Linear(arg_dict["lstm_size"], 164)
        self.fc_pi_a2 = nn.Linear(164, 12)
        self.norm_pi_a1 = nn.LayerNorm(164)
        
        self.fc_pi_m1 = nn.Linear(arg_dict["lstm_size"], 164)
        self.fc_pi_m2 = nn.Linear(164, 8)
        self.norm_pi_m1 = nn.LayerNorm(164)

        self.fc_v1 = nn.Linear(arg_dict["lstm_size"], 164)
        self.norm_v1 = nn.LayerNorm(164)
        self.fc_v2 = nn.Linear(164, 1,  bias=False)
        self.optimizer = optim.Adam(self.parameters(), lr=arg_dict["learning_rate"])
        
    def forward(self, state_dict):
        player_state = state_dict["player"]      #(1,1,29)
        ball_state = state_dict["ball"]           #(1,1,18)
        left_team_state = state_dict["left_team"]
        left_closest_state = state_dict["left_closest"]
        right_team_state = state_dict["right_team"]  
        right_closest_state = state_dict["right_closest"]
        avail = state_dict["avail"]
        
        player_embed = self.norm_player(self.fc_player(player_state))    # (1,1,29) --> (1,1,64)
        ball_embed = self.norm_ball(self.fc_ball(ball_state))            #(1,1,18)  -> (1,1,64)
        left_team_embed = self.norm_left(self.fc_left(left_team_state))  # horizon, batch, n, dim  # (1,1,2,7) --> (1,1,2,48)
        left_closest_embed = self.norm_left_closest(self.fc_left_closest(left_closest_state))      # (1,1,7) --> (1,1,48)
        right_team_embed = self.norm_right(self.fc_right(right_team_state))                        # (1,1,2,7) --> (1,1,2,48)
        right_closest_embed = self.norm_right_closest(self.fc_right_closest(right_closest_state))  # (1,1,7) --> (1,1,48)
        
        [horizon, batch_size, n_player, dim] = left_team_embed.size()  # [1, 1, 2, 48]
        left_team_embed = left_team_embed.view(horizon*batch_size, n_player, dim).permute(0,2,1)         # horizon * batch, dim1, n  (1,48,2)
        left_team_embed = F.relu(self.conv1d_left(left_team_embed)).permute(0,2,1)                       # horizon * batch, n, dim2  (1,2,36)
        left_team_embed = left_team_embed.reshape(horizon*batch_size, -1).view(horizon,batch_size,-1)    # horizon, batch, n * dim2  (1,1,72)
        left_team_embed = F.relu(self.norm_left2(self.fc_left2(left_team_embed)))                        # (1,1,48)
        
        # right_team_embed = right_team_embed.view(horizon*batch_size, n_player+1, dim).permute(0,2,1)    # horizon * batch, dim1, n
        ###rjq
        right_team_embed = right_team_embed.view(horizon*batch_size, n_player, dim).permute(0,2,1)    # horizon * batch, dim1, n      (1,48,2)
        right_team_embed = F.relu(self.conv1d_right(right_team_embed)).permute(0,2,1)                   # horizon * batch, n , dim2   (1,2,36)
        right_team_embed = right_team_embed.reshape(horizon*batch_size, -1).view(horizon,batch_size,-1) # (1,1,72)
        right_team_embed = F.relu(self.norm_right2(self.fc_right2(right_team_embed)))                   # (1,1,96)
        
        cat = torch.cat([player_embed, ball_embed, left_team_embed, right_team_embed, left_closest_embed, right_closest_embed], 2)
        #               (1,1,64)       (1,1,64)    (1,1,96)         (1,1,96)          (1,1,48)            (1,1,48)
        # cat:  (1,1,416)  416=(64+96+48)*2
        cat = F.relu(self.norm_cat(self.fc_cat(cat)))  # (1,1,256)
        h_in = state_dict["hidden"]  #((1,1,256),(1,1,256))
        out, h_out = self.lstm(cat, h_in) # (1,1,256)  ((1,1,256),(1,1,256))
        
        a_out = F.relu(self.norm_pi_a1(self.fc_pi_a1(out)))  #(1,1,12)
        a_out = self.fc_pi_a2(a_out)                         #(1,1,12)
        logit = a_out + (avail-1)*1e7
        prob = F.softmax(logit, dim=2)
        
        prob_m = F.relu(self.norm_pi_m1(self.fc_pi_m1(out)))  #(1,1,8)
        prob_m = self.fc_pi_m2(prob_m)
        prob_m = F.softmax(prob_m, dim=2)  #(1,1,8)

        v = F.relu(self.norm_v1(self.fc_v1(out)))
        v = self.fc_v2(v)

        return prob, prob_m, v, h_out

    def make_batch(self, data):
        # data = [trans1, tr2, ..., trans30] * batch_size
        s_player_batch, s_ball_batch, s_left_batch, s_left_closest_batch, s_right_batch, s_right_closest_batch, avail_batch =  [],[],[],[],[],[],[]
        s_player_prime_batch, s_ball_prime_batch, s_left_prime_batch, s_left_closest_prime_batch, \
                                                  s_right_prime_batch, s_right_closest_prime_batch, avail_prime_batch =  [],[],[],[],[],[],[]
        h1_in_batch, h2_in_batch, h1_out_batch, h2_out_batch = [], [], [], []
        a_batch, m_batch, r_batch, prob_batch, done_batch, need_move_batch = [], [], [], [], [], []
        
        for rollout in data:
            s_player_lst, s_ball_lst, s_left_lst, s_left_closest_lst, s_right_lst, s_right_closest_lst, avail_lst =  [], [], [], [], [], [], []
            s_player_prime_lst, s_ball_prime_lst, s_left_prime_lst, s_left_closest_prime_lst, \
                                                  s_right_prime_lst, s_right_closest_prime_lst, avail_prime_lst =  [], [], [], [], [], [], []
            h1_in_lst, h2_in_lst, h1_out_lst, h2_out_lst = [], [], [], []
            a_lst, m_lst, r_lst, prob_lst, done_lst, need_move_lst = [], [], [], [], [], []
            
            for transition in rollout:
                s, a, m, r, s_prime, prob, done, need_move = transition

                for i in range(len(s)):
                    s_player_lst.append(s[i]["player"])
                    s_ball_lst.append(s[i]["ball"])
                    s_left_lst.append(s[i]["left_team"])
                    s_left_closest_lst.append(s[i]["left_closest"])
                    s_right_lst.append(s[i]["right_team"])
                    s_right_closest_lst.append(s[i]["right_closest"])
                    avail_lst.append(s[i]["avail"])

                    h1_in, h2_in = torch.tensor(s[i]["hidden"][0]).chunk(2, 1)
                    h1_in = h1_in.numpy()
                    h2_in = h2_in.numpy()
                    h1_in_lst.append(h1_in)
                    h2_in_lst.append(h2_in)

                    s_player_prime_lst.append(s_prime[i]["player"])
                    s_ball_prime_lst.append(s_prime[i]["ball"])
                    s_left_prime_lst.append(s_prime[i]["left_team"])
                    s_left_closest_prime_lst.append(s_prime[i]["left_closest"])
                    s_right_prime_lst.append(s_prime[i]["right_team"])
                    s_right_closest_prime_lst.append(s_prime[i]["right_closest"])
                    avail_prime_lst.append(s_prime[i]["avail"])

                    h1_out, h2_out = torch.tensor(s_prime[i]["hidden"][0]).chunk(2, 1)
                    h1_out = h1_out.numpy()
                    h2_out = h2_out.numpy()
                    h1_out_lst.append(h1_out)
                    h2_out_lst.append(h2_out)

                    a_lst.append([a[i]])
                    m_lst.append([m[i]])
                    r_lst.append([r[i]])
                    prob_lst.append([prob[i]])
                    done_mask = 0 if done[i] else 1
                    done_lst.append([done_mask])
                    need_move_lst.append([need_move[i]])
                
            s_player_batch.append(s_player_lst)
            s_ball_batch.append(s_ball_lst)
            s_left_batch.append(s_left_lst)
            s_left_closest_batch.append(s_left_closest_lst)
            s_right_batch.append(s_right_lst)
            s_right_closest_batch.append(s_right_closest_lst)
            avail_batch.append(avail_lst)
            h1_in_batch.append(h1_in_lst[0])
            h2_in_batch.append(h2_in_lst[0])
            
            s_player_prime_batch.append(s_player_prime_lst)
            s_ball_prime_batch.append(s_ball_prime_lst)
            s_left_prime_batch.append(s_left_prime_lst)
            s_left_closest_prime_batch.append(s_left_closest_prime_lst)
            s_right_prime_batch.append(s_right_prime_lst)
            s_right_closest_prime_batch.append(s_right_closest_prime_lst)
            avail_prime_batch.append(avail_prime_lst)
            h1_out_batch.append(h1_out_lst[0])
            h2_out_batch.append(h2_out_lst[0])

            a_batch.append(a_lst)
            m_batch.append(m_lst)
            r_batch.append(r_lst)
            prob_batch.append(prob_lst)
            done_batch.append(done_lst)
            need_move_batch.append(need_move_lst)
        

        s = {
          "player": torch.tensor(s_player_batch, dtype=torch.float, device=self.device).permute(1,0,2),
          "ball": torch.tensor(s_ball_batch, dtype=torch.float, device=self.device).permute(1,0,2),
          "left_team": torch.tensor(s_left_batch, dtype=torch.float, device=self.device).permute(1,0,2,3),
          "left_closest": torch.tensor(s_left_closest_batch, dtype=torch.float, device=self.device).permute(1,0,2),
          "right_team": torch.tensor(s_right_batch, dtype=torch.float, device=self.device).permute(1,0,2,3),
          "right_closest": torch.tensor(s_right_closest_batch, dtype=torch.float, device=self.device).permute(1,0,2),
          "avail": torch.tensor(avail_batch, dtype=torch.float, device=self.device).permute(1,0,2),
          "hidden" : (torch.tensor(h1_in_batch, dtype=torch.float, device=self.device).squeeze(1).permute(1,0,2), 
                      torch.tensor(h2_in_batch, dtype=torch.float, device=self.device).squeeze(1).permute(1,0,2))
        }

        s_prime = {
          "player": torch.tensor(s_player_prime_batch, dtype=torch.float, device=self.device).permute(1,0,2),
          "ball": torch.tensor(s_ball_prime_batch, dtype=torch.float, device=self.device).permute(1,0,2),
          "left_team": torch.tensor(s_left_prime_batch, dtype=torch.float, device=self.device).permute(1,0,2,3),
          "left_closest": torch.tensor(s_left_closest_prime_batch, dtype=torch.float, device=self.device).permute(1,0,2),
          "right_team": torch.tensor(s_right_prime_batch, dtype=torch.float, device=self.device).permute(1,0,2,3),
          "right_closest": torch.tensor(s_right_closest_prime_batch, dtype=torch.float, device=self.device).permute(1,0,2),
          "avail": torch.tensor(avail_prime_batch, dtype=torch.float, device=self.device).permute(1,0,2),
          "hidden" : (torch.tensor(h1_out_batch, dtype=torch.float, device=self.device).squeeze(1).permute(1,0,2), 
                      torch.tensor(h2_out_batch, dtype=torch.float, device=self.device).squeeze(1).permute(1,0,2))
        }

        a,m,r,done_mask,prob,need_move = torch.tensor(a_batch, device=self.device).permute(1,0,2), \
                                         torch.tensor(m_batch, device=self.device).permute(1,0,2), \
                                         torch.tensor(r_batch, dtype=torch.float, device=self.device).permute(1,0,2), \
                                         torch.tensor(done_batch, dtype=torch.float, device=self.device).permute(1,0,2), \
                                         torch.tensor(prob_batch, dtype=torch.float, device=self.device).permute(1,0,2), \
                                         torch.tensor(need_move_batch, dtype=torch.float, device=self.device).permute(1,0,2)

        return s, a, m, r, s_prime, done_mask, prob, need_move

    # def make_batch2(self, data):

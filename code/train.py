from game import SignalingBanditsGame
from arguments import get_args
from lang_agent import LanguageAgent
from demo_agent import DemoAgent
from bayesian_model import *

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

import numpy as np
import pandas as pd

from scipy.special import comb

from statistics import mean
from collections import defaultdict
import time
import os
import copy

import plotly.express as px


def handle_messages(batch_size,
                    i,
                    agent_i, 
                    agent_view, 
                    prev_lang_i,
                    lang_i, 
                    lang_len_i, 
                    games_for_eval,
                    messages_to_log,
                    reward_matrices_views_to_log,
                    args):
    """
    Helper function to handle the core iterated learning portion of the learning
    through messages setting

    """
    # lang_i is shape (batch_size, message_len, vocab_size, num_gens_to_iterate_over)
    lang_i, lang_len_i, scores_i = agent_i(reward_matrices=agent_view,
                                                            input_lang=lang_i,
                                                            input_lang_len=lang_len_i,
                                                            games=games_for_eval
                                                        )
                
    # append flattened message produced by agent i
    flattened_message = lang_i.view(batch_size, -1).cpu().detach().numpy()
    messages_to_log.append(flattened_message)

    # append flattened view of reward matrices seen by agent i
    flattened_agent_view = agent_view.view(batch_size, -1).cpu().detach().numpy()
    reward_matrices_views_to_log.append(flattened_agent_view)

    if args.ingest_multiple_messages:
        # take lang_i off the gpu and unsqueeze
        lang_i = lang_i.unsqueeze(-1).cpu()
        if prev_lang_i is not None: prev_lang_i = prev_lang_i.cpu()

        if prev_lang_i is not None:
            lang_i = torch.cat([prev_lang_i, lang_i], dim=-1)   # shape (batch_size, message_len, vocab_size, gen_i)
            # else, there is nothing to append

        # fill up the rest of the message with zeros
        # note: even when doing chain length extrapolation the message should be size args.chain_length,
        # not args.train_length
        empty_message_size = (lang_i.shape[0], lang_i.shape[1], lang_i.shape[2], args.chain_length-i-1)
        empty_message = torch.zeros(size=empty_message_size)    # shape (batch_size, message_len, vocab_size, gen_i)

        lang_i = torch.cat([lang_i, empty_message], dim=-1)

        # only keep the nonzero messages
        prev_lang_i = lang_i[:, :, :, :i+1]

    if args.cuda:
        lang_i = lang_i.cuda()
        if args.discrete_comm: lang_len_i = lang_len_i.cuda()

    return prev_lang_i, lang_i, lang_len_i, scores_i


def run_epoch(dataset_split, game, agents, optimizer, args):
    """
    Arguments:
    dataset_split: string
    game: np.array of size (batch_size, num_choices, object_encoding_len)
    agents: list of Agent objects
    optimizer: PyTorch optimizer object
    args: 

    Return:
    metrics: dict
    """
    training = dataset_split == 'train'

    batch_rewards_after_agent_i = [[] for _ in range(args.chain_length)]
    batch_losses_after_agent_i = [[] for _ in range(args.chain_length)]
    batch_accuracy_after_agent_i = [[] for _ in range(args.chain_length)]
    #batch_teacher_dist = {i:[] for i in [1, 10, 100, 1000, 10000]}
    #batch_teacher_mean_score = {i:[] for i in [1, 10, 100, 1000, 10000]}
    batch_num_unique_teacher_demos = []
    #batch_num_unique_teacher_messages = []
    
    data_to_log = []
    for batch_idx in range(args.num_batches_per_epoch):
        messages_to_log = []
        reward_matrices_to_log = []
        reward_matrices_views_to_log = []

        
        reward_matrices, games_for_eval, all_possible_games, reward_assignments = game.sample_batch(inductive_bias=(training and args.inductive_bias),
                                                                                split=dataset_split
                                                                                )

        batch_size = reward_matrices.shape[0]

        # append flattened ground truth reward matrix
        flattened_reward_matrices = reward_matrices.reshape(batch_size, -1)
        reward_matrices_to_log.append(flattened_reward_matrices)

        # convert reward matrices and listener views to tensors
        reward_matrices = torch.from_numpy(reward_matrices).float()
        games_for_eval = torch.from_numpy(games_for_eval).float()
        all_possible_games = torch.from_numpy(all_possible_games).float()
        
        if args.partial_reward_matrix:
            num_utilities_seen_in_training = None
            if args.num_utilities_seen_in_training is not None and training:
                num_utilities_seen_in_training = args.num_utilities_seen_in_training

            reward_matrices_views = game.generate_masked_reward_matrix_views(reward_matrices, 
                                                                                chunks=args.chunks,
                                                                                num_views=args.chain_length,
                                                                                same_agent_view=args.same_agent_view,
                                                                                no_additional_info=args.no_additional_info,
                                                                                num_utilities_seen_in_training=num_utilities_seen_in_training)    
        
        if args.cuda:   # move to GPU
            reward_matrices = reward_matrices.cuda()
            games_for_eval = games_for_eval.cuda()
            #all_possible_games = all_possible_games.cuda()
            
            if args.partial_reward_matrix:
                reward_matrices_views = reward_matrices_views.cuda()

        if args.learn_from_demos:
            prev_demo_i = None
        else:
            prev_lang_i = None
            lang_i = None
            lang_len_i = None

        losses_for_curr_batch = []
        if training and args.train_chain_length is not None:
            num_gens_to_iterate_over = args.train_chain_length
        else:
            num_gens_to_iterate_over = args.chain_length

        # these are the indices of the agents that we randomly select to be in the training chain
        random_indices = np.random.choice(a=args.chain_length, size=num_gens_to_iterate_over, replace=True)
        for i in range(num_gens_to_iterate_over):
            
            if args.shuffle_agents:
                random_i = random_indices[i]
                agent_i = agents[random_i]
            else:
                agent_i = agents[i]
            
            # what view is the agent seeing?
            if args.partial_reward_matrix:
                agent_view = reward_matrices_views[i]

            elif args.perfect_teacher:
                if i == 0:  # teacher gets all the utilities
                    agent_view = reward_matrices
                if i == 1:  # student gets no utilities
                    agent_view = torch.zeros_like(reward_matrices)
            else:
                agent_view = reward_matrices

            if args.learn_from_demos:
                if args.use_bayesian_teacher and i == 0:
                    demo_i, scores_i = bayesian_teacher(all_possible_games=all_possible_games,
                                                                            is_first_agent=prev_demo_i is None, 
                                                                            reward_matrices=reward_matrices,
                                                                            game=game,
                                                                            teacher_alpha=100,
                                                                            games_for_eval=games_for_eval)
                
                else:
                    demo_i, scores_i, neural_game_scores = agent_i(reward_matrices=agent_view,
                                                                demos=prev_demo_i,
                                                                all_possible_games=all_possible_games,
                                                                games_for_eval=games_for_eval)

                
                
                if args.pedagogical_sampling and not args.use_bayesian_teacher:
                    if i == 0:  # only do this for the teacher
                        # compute the number of unique demos generated in the batch
                        num_unique_demos = len(set(neural_game_scores.argmax(-1).cpu().numpy()))
                        batch_num_unique_teacher_demos.append(num_unique_demos)
                        """
                        # compute alignment with Bayesian model for a range of teacher alpha vals
                        
                        for teacher_alpha in batch_teacher_mean_score.keys():
                            
                            bayesian_demo_scores = compute_demo_score(all_possible_games=all_possible_games,
                                                                            is_first_agent=prev_demo_i is None, 
                                                                            reward_matrices=reward_matrices,
                                                                            game=game,
                                                                            teacher_alpha=teacher_alpha)
                            #breakpoint()
                            corr = compute_correlation(neural_game_scores, bayesian_demo_scores)
                            mean_bayesian_score = compute_mean_bayesian_score(neural_game_scores, bayesian_demo_scores)

                            batch_teacher_dist[teacher_alpha].append(corr)
                            batch_teacher_mean_score[teacher_alpha].append(mean_bayesian_score)
                        """

                # update the demos for the next generation to ingest
                prev_demo_i = demo_i
                
            else:
                prev_lang_i, lang_i, lang_len_i, scores_i = handle_messages(batch_size,
                                                                i,
                                                                agent_i, 
                                                                agent_view, 
                                                                prev_lang_i,
                                                                lang_i, 
                                                                lang_len_i, 
                                                                games_for_eval,
                                                                messages_to_log,
                                                                reward_matrices_views_to_log,
                                                                args)
            
            if i == 0:  # save for logging
                if args.learn_from_demos: 
                    signals = demo_i
                else:
                    signals = lang_i

            # get the listener predictions
            preds = torch.argmax(scores_i, dim=-1)    # (batch_size)

            # get the rewards associated with the objects in each game
            game_rewards = game.compute_rewards(games_for_eval, reward_matrices)  # (batch_size, num_choices)

            # move to GPU if necessary
            game_rewards = game_rewards.to(reward_matrices.device)

            # what reward did the model actually earn
            model_rewards = game_rewards.gather(-1, preds.unsqueeze(-1))

            # what is the maximum reward and the associated index
            max_rewards, argmax_rewards = game_rewards.max(-1)

            avg_reward = model_rewards.squeeze(-1).mean().item()
            avg_max_reward = max_rewards.mean().item()

            accuracy = (argmax_rewards == preds).float().mean().item()

            # minimize negative expected reward
            losses = -1 * torch.einsum('bvc,bvc->bv',(scores_i.exp(), game_rewards))    # size (batch_size, num_views)
            loss = losses.mean()    # take the mean over all views and each reward matrix in the batch

            batch_rewards_after_agent_i[i].append(avg_reward / avg_max_reward)
            batch_losses_after_agent_i[i].append(loss.item())
            batch_accuracy_after_agent_i[i].append(accuracy)

            losses_for_curr_batch.append(loss)

            # if we're in the last batch and also the teacher (agent 0), grab an assortment of demos
            if batch_idx == args.num_batches_per_epoch - 1 and i == 0:
                if args.learn_from_demos and args.pedagogical_sampling:
                    # get the first 20 and convert to numpy
                    demos_subset = demo_i[:20].detach().cpu().numpy()
                

        if training:    # use the losses from all the agents
            if args.optimize_jointly:
                loss_across_agents = torch.mean(torch.stack(losses_for_curr_batch))
                loss_across_agents.backward()
                optimizer.step()
            else:
                for i in range(num_gens_to_iterate_over):
                    #breakpoint()
                    if not (i == 0 and args.use_bayesian_teacher):
                        # loss for agent i = loss for agent i+1 + ... + loss for agent n
                        loss = torch.sum(torch.stack(losses_for_curr_batch)[i:])
                        loss.backward(retain_graph=True)    # need to retain computation graph bc of the way we compute the loss

                        if args.shuffle_agents:
                            agent_i = random_indices[i]
                            optimizer[agent_i].step()
                        else:
                            optimizer[i].step()


        for idx in range(args.batch_size):
            reward_assignment = reward_assignments[idx]
            reward_matrix = reward_matrices[idx].detach().cpu().numpy()
            signal = signals[i].detach().cpu().numpy()
            
            data_to_log.append([reward_assignment, reward_matrix, signal])
        
    
    print(dataset_split)
    
    metrics = {}
    for i in range(num_gens_to_iterate_over):
        metrics['reward_' + str(i)] = mean(batch_rewards_after_agent_i[i])
        metrics['loss_' + str(i)] = mean(batch_losses_after_agent_i[i])
        metrics['accuracy_' + str(i)] = mean(batch_accuracy_after_agent_i[i])

        print('metrics after passing through agent', i)
        print('reward:', metrics['reward_' + str(i)])
        print('loss:', metrics['loss_' + str(i)])
        print('accuracy:', metrics['accuracy_' + str(i)])

    if args.learn_from_demos and args.pedagogical_sampling and not args.use_bayesian_teacher:
        metrics['num_unique_demos'] = mean(batch_num_unique_teacher_demos)
        print('num unique demos:', metrics['num_unique_demos'])

        #for teacher_alpha in batch_teacher_dist.keys():
        #    metrics['jsd_alpha={}'.format(teacher_alpha)] = mean(batch_teacher_dist[teacher_alpha])
        #    metrics['mean Bayesian score_alpha={}'.format(teacher_alpha)] = mean(batch_teacher_mean_score[teacher_alpha])

            #print('jsd:', metrics['jsd_alpha={}'.format(teacher_alpha)])
            #print('mean Bayesian score:', metrics['mean Bayesian score_alpha={}'.format(teacher_alpha)])
        
    # log
    col_names = ['reward_assignment', 'reward_matrix', 'signal']
    df = pd.DataFrame(data_to_log, columns=col_names)

    return metrics, df

def main():
    args = get_args()
    if args.learn_from_demos:
        num_objects = args.num_colors * args.num_shapes
        num_possible_demos = int(comb(num_objects, 3))
        agent = DemoAgent(chain_length=args.chain_length,
                            pedagogical_sampling=args.pedagogical_sampling,
                            num_possible_demos=num_possible_demos,
                            object_encoding_len=args.num_colors + args.num_shapes, 
                            num_examples_for_demos=args.num_examples_for_demos,
                            num_objects=num_objects
                        )
    else:
        agent = LanguageAgent(chain_length=args.chain_length,
                        object_encoding_len = args.num_colors + args.num_shapes,
                        num_objects=args.num_colors * args.num_shapes,
                        hidden_size=args.hidden_size,
                        use_discrete_comm=args.discrete_comm,
                        max_message_len=args.max_message_len,
                        vocab_size=args.vocab_size,
                        ingest_multiple_messages=args.ingest_multiple_messages)
    
    if args.use_same_agent:
        agents = nn.ModuleList([agent for _ in range(args.chain_length)])
    else:
        agents = nn.ModuleList([copy.deepcopy(agent) for _ in range(args.chain_length)])

    if args.cuda:
        agents = nn.ModuleList([agent.cuda() for agent in agents])

    if args.optimize_jointly:
        optimizer = optim.Adam(agents.parameters(),
                           lr=args.lr)
    else:
        optimizer = [optim.Adam(agent.parameters(), lr=args.lr)
                        for agent in agents]

    metrics = defaultdict(list)
    game = SignalingBanditsGame(num_colors=args.num_colors, 
                                num_shapes=args.num_shapes, 
                                train_percent=args.train_percent)

    if args.wandb:
        import wandb
        if args.group is not None:
            wandb.init(args.wandb_project_name, group=args.group, config=args)
        else:
            wandb.init(args.wandb_project_name, config=args)

        if args.name is not None:
            wandb.run.name = args.name
        else:
            args.name = wandb.run.name

    # initialize a dataframe to log reward of all n generations
    df = pd.DataFrame(columns=['gen', 'val_reward', 'epoch'])

    for i in range(args.epochs):
        print('epoch', i)
        start = time.time()
        
        train_metrics, train_df = run_epoch('train', game, agents, optimizer, args)
        val_metrics, val_df = run_epoch('val', game, agents, optimizer, args)
        
        # just save the validation set, and rewrite it after each iter
        if args.save_outputs:
            if not os.path.exists(args.save_dir):
                os.makedirs(args.save_dir)

            train_pkl_full_save_path = os.path.join(args.save_dir, args.name + '_train.pkl')
            train_df.to_pickle(train_pkl_full_save_path)

            val_pkl_full_save_path = os.path.join(args.save_dir, args.name + '_val.pkl')
            val_df.to_pickle(val_pkl_full_save_path)
        
        for metric, value in train_metrics.items():
            metrics['train_{}'.format(metric)] = value

        for metric, value in val_metrics.items():
            metrics['val_{}'.format(metric)] = value

        metrics['current_epoch'] = i
        end = time.time()
        print('elapsed time:', end-start)

        # add validation reward to dataframe
        for gen in range(args.chain_length):
            entry = [gen, metrics['val_reward_'+str(gen)], metrics['current_epoch']]
            df.loc[len(df.index)] = entry

        # create plotly object
        generation_plot = px.line(df, x='epoch', y='val_reward', color='gen', markers=True)

        if args.wandb:
            wandb.log(metrics)  # log standard metrics
            wandb.log({'generation_plot': generation_plot}) # log side-by-side comparison of reward across generations
            #wandb.log({'demos_subset_agent_0': wandb.Table(dataframe=val_demos_df)})
        
        
if __name__ == "__main__":
    main()
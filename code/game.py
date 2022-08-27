import numpy as np
import itertools
import torch
import time

class SimpleGame():
    def __init__(self, num_choices=3):
        self.num_choices = num_choices

    def sample_utilities(self):
        """
        Randomly select utilities for a single game

        Return:
        utilities: np.array of size (num_choices,)
        """
        utilities = np.random.choice(10, size=self.num_choices, replace=False) + 1
        return utilities.astype(np.float32) / utilities.max()

    def generate_batch(self, batch_size=32):
        """
        Generate games for a batch

        Return:
        batch: np.array of size (batch_size, num_choices)
        """
        batch = []
        for _ in range(batch_size):
            batch.append(self.sample_utilities())
        return np.array(batch)

    def compute_rewards(self, utilities, choices):
        """
        Given a batch of utilities and agent choices, compute the rewards for each agent.

        Return
        rewards: np.array of size (batch_size,)
        """
        rewards = utilities.gather(-1, choices)
        return rewards


class SignalingBanditsGame():
    def __init__(self, num_choices=3, 
                    num_colors=3, num_shapes=3, 
                    max_color_val=2, max_shape_val=1
                ):
        self.num_choices = num_choices
        self.num_colors = num_colors
        self.num_shapes = num_shapes

        color_utilities = np.linspace(start=-max_color_val, stop=max_color_val, num=num_colors)
        shape_utilities = np.linspace(start=-max_shape_val, stop=max_shape_val, num=num_shapes)
        
        color_orderings = list(itertools.permutations(color_utilities))
        shape_orderings = list(itertools.permutations(shape_utilities))

        #all_reward_assignments = list(itertools.product(color_orderings, shape_orderings))
        self.possible_reward_assignments = list(itertools.product(color_orderings, shape_orderings))
        self.num_reward_assignments = len(self.possible_reward_assignments)

        num_objects = num_colors * num_shapes 
        # all possible listener contexts
        self.combinations = list(itertools.combinations(range(num_objects), r=num_choices))

    def sample_reward_matrix(self):
        """
        Generate the reward matrix, to which the speaker will gain access

        Arguments:
        None

        Return:
        reward_matrix: np.array of size (self.num_colors*self.num_shapes, self.num_colors + self.num_shapes + 2)
        The first (self.num_colors + self.num_shapes) items in a row are the object embedding
        The next item is the utility associated with that object
        The next item is a Boolean representing whether that object is in the listener's context

        i.e. if the item described by the feature [0, 1, 0, 1, 0, 0] has utility 3 and is present in the listener's
        context, then the corresponding row in reward_matrix is [0, 1, 0, 1, 0, 0, 3, 1]
        """
        # determines the reward configuration we are using
        reward_assignment_idx = np.random.randint(0, self.num_reward_assignments)
        reward_assignment = self.possible_reward_assignments[reward_assignment_idx]
        color_utilities, shape_utilities = reward_assignment

        # determines objects that appear in the listener context
        #indices = np.random.choice(self.num_colors*self.num_shapes, size=self.num_choices, replace=False)
        
        curr_idx = 0
        reward_matrix = []
        for i in range(self.num_colors):
            for j in range(self.num_shapes):
                color_embedding = np.zeros(shape=(self.num_colors,))
                color_embedding[i] = 1
                color_utility = color_utilities[i]

                shape_embedding = np.zeros(shape=(self.num_shapes,))
                shape_embedding[j] = 1
                shape_utility = shape_utilities[j]

                embedding = np.concatenate((color_embedding, shape_embedding))
                #in_listener_context = int(curr_idx in indices)
                #embedding = np.append(embedding, [color_utility+shape_utility, in_listener_context])
                embedding = np.append(embedding, [color_utility+shape_utility])
                #breakpoint()
                reward_matrix.append(embedding)

                curr_idx += 1
    
        return np.array(reward_matrix)

    def get_multiple_listener_views(self, reward_matrix, num_views):
        """
        Given a reward matrix, get the specified number of possible listener views.
        For a default game with 9 objects and 3 objects in the context, the max
        number of possible views is 9 choose 3 = 84
        Arguments:
        reward_matrix: np.array of size (self.num_colors*self.num_shapes, self.num_colors + self.num_shapes + 2)
        Return:
        listener_views: np.array of size (num_combinations, num_choices, self.num_colors + self.num_shapes)
        """
        
        indices_of_combinations = np.random.choice(a=len(self.combinations), size=num_views, replace=False)

        listener_views = []
        for idx in indices_of_combinations:
            combo = self.combinations[idx]
            view = reward_matrix[combo, :-1] # lop off the last element (the reward)
            listener_views.append(view)
            
        return np.array(listener_views)

    def sample_batch(self, num_listener_views, num_examples_for_demos, batch_size=32):
        """
        Sample games for a whole batch
        Arguments:
        num_listener_views: int
        batch_size: int

        Return
        batch_reward_matrices: np.array of size (batch_size, self.num_colors*self.num_shapes, self.num_colors+self.num_shapes+1)
        batch_listener_views: np.array of size (batch_size, self.num_choices, self.num_colors+self.num_shapes)
        """
        batch_reward_matrices = []
        batch_eval_listener_views = []
        batch_demo_listener_views = []
        
        for i in range(batch_size):
            reward_matrix = self.sample_reward_matrix()
            eval_listener_views = self.get_multiple_listener_views(reward_matrix, num_listener_views)
            demo_listener_views = self.get_multiple_listener_views(reward_matrix, num_examples_for_demos)
   
            batch_reward_matrices.append(reward_matrix)
            batch_eval_listener_views.append(eval_listener_views)
            batch_demo_listener_views.append(demo_listener_views)
        
        return np.array(batch_reward_matrices), np.array(batch_eval_listener_views), np.array(batch_demo_listener_views)

    def compute_rewards(self, listener_views, reward_matrices):
        """
        Given a batch of games and model predictions, compute the rewards

        Arguments:
        listener_views: np.array of size (batch_size, num_views, self.num_choices, self.num_colors+self.num_shapes)
        reward_matrices: np.array of size (batch_size, self.num_colors*self.num_shapes, self.num_colors+self.num_shapes+2)

        Return:
        reshaped_rewards: torch.Tensor of size (batch_size, num_listener_views, self.num_choices)
        """

        batch_size = reward_matrices.shape[0]
        indices_of_ones = listener_views.nonzero()
        indices_of_ones_combined = indices_of_ones.view(indices_of_ones.shape[0] // 2, -1)
        # an example row is:
        # [batch_i, listener_view_j, object_in_context_k, 2, batch_i, listener_view_j, object_in_context_k, 4]
        # which means that in the ith data point in the batch, for the jth listener_view, for the kth object in the listener context,
        # there is a 1 at index 2 and index 4

        indices_into_batch = indices_of_ones_combined[:, 0]

        # arithmetic trick to derive the index into the reward matrix
        indices_into_reward_matrix_row = self.num_colors*indices_of_ones_combined[:, self.num_colors-1] + \
                                        (indices_of_ones_combined[:, -1]-self.num_shapes)

        # note that we only need the index of the sample in the batch and the row in the reward matrix
        # and then we pull out the LAST element, which is the reward
        rewards = reward_matrices[indices_into_batch, indices_into_reward_matrix_row, -1]

        # note: I verified that .view() yields the correct arrangement
        rewards_reshaped = rewards.view(batch_size, -1, self.num_choices)

        return rewards_reshaped

    def generate_masked_reward_matrix_views(self, reward_matrices, 
                                                    chunks, 
                                                    num_views,
                                                    same_agent_view,
                                                    no_additional_info,
                                                    num_utilities_seen_in_training):
        """
        For a batch of reward matrices, produce partial views

        Arguments:
        reward_matrices: np.array of size (batch_size, self.num_colors*self.num_shapes, self.num_colors+self.num_shapes+2)
        chunks: list(int) representing the indices at which to split the shuffled indices (i.e. [2, 4] would yield [a[:2], a[2:4], a[4:]])
        num_views: int representing the number of partial views to generate
        same_agent_view: bool to indicate if all agents in the chain see the same k objects
        no_additional_info: bool to indicate if there is no new information provided after the first agent
        num_utilities_seen_in training: if not None, this specifies how many utilities the agent sees

        Return:
        reward_matrix_views: np.array of size (num_views, batch_size, self.num_colors*self.num_shapes, self.num_colors+self.num_shapes+2)
        """

        # generate masks
        batch_size = reward_matrices.shape[0]
        batch_masks = []
        
        for _ in range(batch_size):
            shuffled_indices = np.arange(start=0, stop=reward_matrices.shape[1], dtype=int)
            np.random.shuffle(shuffled_indices)
            
            if chunks is not None:

                if num_utilities_seen_in_training is not None:  # view fewer utilities during training, compared to test
                    splits = np.array_split(shuffled_indices, num_utilities_seen_in_training)
                else:
                    splits = np.array_split(shuffled_indices, chunks)
                splits = splits[:num_views]
            else:
                splits = np.array_split(shuffled_indices, num_views)

            masks = []
            for split in splits:
                mask = list(set(range(reward_matrices.shape[1])) - set(split))
                masks.append(mask)
            batch_masks.append(masks)
        
        # apply masks to reward matrices (only to the first n-1 agents)
        reward_matrices_views = torch.stack([reward_matrices for _ in range(num_views)])
        for sample_idx in range(batch_size):
            for agent_idx in range(num_views):
                mask = batch_masks[sample_idx][agent_idx]
                reward_matrices_views[agent_idx, sample_idx, mask, :] = 0
        
        if same_agent_view:
            # repeat the view of the first agent for all the agents
            reward_matrices_views = reward_matrices_views[0, :, :, :].unsqueeze(0).repeat(num_views, 1, 1, 1)

        if no_additional_info:
            # just do zeros for all agents after agent 0
            zeros = torch.zeros_like(reward_matrices_views)[:-1, :, :, :]
            reward_matrices_views = torch.cat([reward_matrices_views[0, :, :, :].unsqueeze(0), zeros], dim=0)
        
        return reward_matrices_views
            

            







    
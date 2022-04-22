import numpy as np
import torch

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

        self.color_utilities = np.linspace(start=-max_color_val, stop=max_color_val, num=num_colors)
        self.shape_utilities = np.linspace(start=-max_shape_val, stop=max_shape_val, num=num_shapes)

        self.embeddings, self.rewards = self.get_reward_matrix()
        self.reward_matrix = np.concatenate((self.embeddings, self.rewards), dim=1)
        self.embedding2reward = zip(self.embeddings, self.rewards)

    def get_reward_matrix(self):
        """
        Generate the reward matrix, which the speaker will gain access to

        Return:
        reward_matrix: np.array of size(self.num_colors*self.num_shapes, self.num_colors + self.num_shapes + 1)
        i.e. if the item described by the feature [0, 1, 0, 1, 0, 0] has utility 3, then the corresponding row in the matrix
        is [0, 1, 0, 1, 0, 0, 3]
        """
        embeddings = []
        rewards = []
        for i in range(self.num_colors):
            for j in range(self.num_shapes):
                color_embedding = np.zeros(shape=(self.num_colors,))
                color_embedding[i] = 1
                color_utility = self.color_utilities[i]

                shape_embedding = np.zeros(shape=(self.num_shapes,))
                shape_embedding[j] = 1
                shape_utility = self.shape_utilities[j]

                embedding = np.concatenate((color_embedding, shape_embedding))

                embeddings.append(embedding)
                rewards.append(color_utility + shape_utility)

        return np.array(embeddings), np.array(rewards)

    def sample_game(self):
        """
        Sample a single game

        Return:
        game: np.array of size (num_choices, self.num_colors + self.num_shapes)
        """
        num_objects = self.num_colors * self.num_shapes
        indices = np.random.choice(num_objects, size=self.num_choices, replace=False)

        game = self.reward_matrix[indices, -1]  # lop off the last element bc that's the reward
        return game

    def sample_batch(self, batch_size=32):
        """
        Sample games for a whole batch

        Return
        batch_games: np.array of size(batch_size, self.num_choices, self.num_colors + self.num_shapes)
        """
        batch_games = np.array(shape=(batch_size, self.num_choices, self.num_colors + self.num_shapes))
        for i in range(batch_size):
            game = self.sample_game()
            batch_games[i, :, :] = game

        return batch_games

    def compute_reward(self, games, preds):
        """
        Given a batch of games and model prediction, compute the rewards

        Return:
        accuracy: np.array of size (batch_size)
        """
        batch_size = games.shape[0]
        batch_reward = np.array((batch_size))

        for i in range(batch_size):
            game_utilities = games[i]
            game_rewards = np.array((self.num_choices,))
            for j in range(self.num_choices):
                game_rewards[j] = self.embedding2reward[game_utilities[j]]

            prediction_idx = preds[i]
            predicted_object = games[i, prediction_idx, :]
            reward = self.embedding2reward[predicted_object]
            reward_normalized = reward / game_rewards.max()

            batch_reward[i] = reward_normalized

        return batch_reward

    def get_ground_truth(self, games):
        """
        Given a batch of games, return the indices of the object in each game that 
        has the highest associated utility

        Return:
        ground_truth: np.array of size (batch_size)
        """
        batch_size = games.shape[0]
        ground_truth = np.array((batch_size))
        
        for i in range(batch_size):
            game_utilities = games[i]
            game_rewards = np.array((self.num_choices,))
            for j in range(self.num_choices):
                game_rewards[j] = self.embedding2reward[game_utilities[j]]
            
            idx = np.argmax(game_rewards)
            ground_truth[i] = idx

        return ground_truth
            

            







    
import random
import logging
import argparse
import numpy as np
import concurrent.futures


from Net.NNet import NNetWrapper
from MCTS import MCTS, hash_ndarray
from Othello import OthelloGame, OthelloPlayer, BoardView

LOG_FORMAT = '%(asctime)s %(levelname)s: %(message)s'
DEFAULT_CHECKPOINT_FILEPATH = './othelo_model.weights'


class OthelloMCTS(MCTS):
    def __init__(self, board_size, neural_network, degree_exploration):
        self._board_size = board_size
        self._neural_network = neural_network
        self._predict_cache = {}

        super().__init__(degree_exploration)
    
    def is_terminal_state(self, state):
        return OthelloGame.has_board_finished(state)
    
    def get_state_value(self, state):
        return self._neural_network_predict(state)[1]

    def get_state_reward(self, state):
        return OthelloGame.get_board_winning_player(state)[0].value

    def get_state_actions_propabilities(self, state):
        return self._neural_network_predict(state)[0]
    
    def get_state_actions(self, state):
        return [tuple(a) for a in OthelloGame.get_player_valid_actions(state, OthelloPlayer.BLACK)]
    
    def get_next_state(self, state, action):
        board = np.copy(state)
        next_state = OthelloGame.flip_board_squares(board, OthelloPlayer.BLACK, *action)

        if OthelloGame.has_player_actions_on_board(board, OthelloPlayer.WHITE):
            # Invert board to keep using BLACK perspective
            return OthelloGame.invert_board(board)
        return board

    def get_policy_action_probabilities(self, state, temperature):
        probabilities = np.zeros((self._board_size, self._board_size))

        if temperature == 0:
            for action in self._get_state_actions(state):
                row, col = action
                probabilities[row, col] = self.N(state, action)
            bests = np.argwhere(probabilities == probabilities.max())
            row, col = random.choice(bests)
            probabilities = np.zeros((self._board_size, self._board_size))
            probabilities[row, col] = 1
            return probabilities

        for action in self._get_state_actions(state):
            row, col = action
            probabilities[row, col] = self.N(state, action) ** (1 / temperature)
        
        return probabilities / np.sum(probabilities)

    def _neural_network_predict(self, state):
        hash_ = hash_ndarray(state)
        if hash_ not in self._predict_cache:
            self._predict_cache[hash_] = self._neural_network.predict(state)
        return self._predict_cache[hash_]


def execute_episode(board_size, neural_network, degree_exploration, num_simulations, policy_temperature):
    examples = []
    
    game = OthelloGame(board_size)

    mcts = OthelloMCTS(board_size, neural_network, degree_exploration)

    while not game.has_finished(): 
        state = game.board(BoardView.TWO_CHANNELS)
        for _ in range(num_simulations):
            mcts.simulate(state)

        policy = mcts.get_policy_action_probabilities(state, policy_temperature)
        
        if game.current_player == OthelloPlayer.WHITE:
            state = OthelloGame.invert_board(state)

        example = state, policy, game.current_player
        examples.append(example)

        action = np.argwhere(policy == policy.max())[0]
        
        game.play(*action)

    winner, winner_points = game.get_winning_player()

    return [(state, policy, 1 if winner == player else -1) for state, policy, player in examples]


def duel_between_neural_networks(board_size, neural_network_1, neural_network_2):
    game = OthelloGame(board_size)

    players_neural_networks = {
        OthelloPlayer.BLACK: neural_network_1,
        OthelloPlayer.WHITE: neural_network_2
    }

    while not game.has_finished():
        nn = players_neural_networks[game.current_player]
        action_probabilities, state_value = nn.predict(game.board(BoardView.TWO_CHANNELS))
        valid_actions = game.get_valid_actions()
        best_action = max(valid_actions, key=lambda position: action_probabilities[tuple(position)])
        game.play(*best_action)
        print(game.board())

    return game.get_winning_player()[0]


def training(board_size, num_iterations, num_episodes, num_simulations, degree_exploration, temperature,
             total_games, victory_threshold, neural_network, temperature_threshold=None, 
             checkpoint_filepath=None, episode_thread_pool=1, game_thread_pool=1):
    training_examples = []
    for i in range(1, num_iterations + 1):
        old_neural_network = neural_network.copy()
        
        logging.info(f'Iteration {i}/{num_iterations}: Starting iteration')
        
        if temperature_threshold and i >= temperature_threshold:
            logging.info(f'Iteration {i}/{num_iterations}: Temperature threshold reached, '
                          'changing temperature to 0')
            temperature = 0

        logging.info(f'Iteration {i}/{num_iterations} - Generating episodes')

        with concurrent.futures.ThreadPoolExecutor(max_workers=episode_thread_pool) as executor:
            future_results = {}
            
            for e in range(1, num_episodes + 1):
                future_result = executor.submit(execute_episode, board_size, neural_network, degree_exploration, 
                                                num_simulations, temperature)
                future_results[future_result] = e

            logging.info(f'Iteration {i}/{num_iterations} - Waiting for episodes results')

            for future in concurrent.futures.as_completed(future_results):
                e = future_results[future]
                logging.info(f'Iteration {i}/{num_iterations} - Episode {e}: Finished')
                episode_examples = future.result()
                training_examples.extend(episode_examples)

        logging.info(f'Iteration {i}/{num_iterations}: All episodes finished')
        
        training_verbose = 2 if logging.root.level <= logging.DEBUG else None

        logging.info(f'Iteration {i}/{num_iterations}: Training model with episodes examples')
        logging.debug(f'training_verbose={training_verbose}')

        history = neural_network.train(training_examples, verbose=training_verbose)

        logging.info(f'Iteration {i}/{num_iterations}: Saving trained model in "{checkpoint_filepath}"')

        neural_network.save_checkpoint(checkpoint_filepath)

        logging.info(f'Iteration {i}/{num_iterations}: Self-play to evaluate the neural network training')

        new_net_victories = 0
        
        logging.info(f'Iteration {i}/{num_iterations} - Generating matches')

        with concurrent.futures.ThreadPoolExecutor(max_workers=game_thread_pool) as executor:
            future_results = {}
            
            for g in range(1, total_games + 1):
                future_result = executor.submit(duel_between_neural_networks, board_size, 
                                                old_neural_network, neural_network)
                future_results[future_result] = g

            logging.info(f'Iteration {i}/{num_iterations} - Waiting for matches results')

            for future in concurrent.futures.as_completed(future_results):
                g = future_results[future]
                winner = future.result()
                if winner is neural_network:
                    logging.info(f'Iteration {i}/{num_iterations} - Game {g}/{total_games}: New neural network has won')
                    new_net_victories += 1
                else:
                    logging.info(f'Iteration {i}/{num_iterations} - Game {g}/{total_games}: New neural network has lost')

                logging.info(f'Iteration {i}/{num_iterations} - Game {g}/{total_games}: ' 
                            f'Promotion status ({new_net_victories}/{victory_threshold})')

        if new_net_victories >= victory_threshold:
            logging.info(f'Iteration {i}/{num_iterations}: New neural network has been promoted')
        else:
            neural_network = old_neural_network

if __name__ == '__main__':
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-b', '--board-size', default=6, type=int, help='Othello board size')
    parser.add_argument('-i', '--iterations', default=80, type=int, help='Number of training iterations')
    parser.add_argument('-e', '--episodes', default=100, type=int, help='Number of episodes by iterations')
    parser.add_argument('-s', '--simulations', default=25, type=int, help='Number of MCTS simulations by episode')
    parser.add_argument('-g', '--total-games', default=10, type=int, help='Total of games to evaluate neural network training')
    parser.add_argument('-v', '--victory-threshold', default=6, type=int, help='Number of victories to promote neural network training')
    parser.add_argument('-c', '--constant-upper-confidence', default=1, type=int, help='MCTS upper confidence bound constant')

    parser.add_argument('-ep', '--epochs', default=10, type=int, help='Number of epochs for neural network training')
    parser.add_argument('-lr', '--learning-rate', default=0.001, type=float, help='Neural network training learning rate')
    parser.add_argument('-dp', '--dropout', default=0.3, type=float, help='Neural network training dropout')
    parser.add_argument('-bs', '--batch-size', default=32, type=int, help='Neural network training batch size')
    
    parser.add_argument('-et', '--episode-threads', default=1, type=int, help='Number of episodes to be executed asynchronously')
    parser.add_argument('-gt', '--game-threads', default=1, type=int, help='Number of games to be executed asynchronously '
                                                                           'during evaluation')

    parser.add_argument('-o', '--output-file', default=DEFAULT_CHECKPOINT_FILEPATH, help='File path to save neural network weights')
    parser.add_argument('-w', '--weights-file', default=None, help='File path to load neural network weights')
    parser.add_argument('-l', '--log-level', default='DEBUG', choices=('INFO', 'DEBUG', 'WARNING', 'ERROR'), help='Logging level')
    parser.add_argument('-t', '--temperature', default=1, type=int, help='Policy temperature parameter')
    parser.add_argument('-tt', '--temperature-threshold', default=25, type=int, help='Number of iterations using the temperature '
                                                                                     'parameter before changing to 0')
    
    args = parser.parse_args()

    assert args.victory_threshold < args.total_games, '"victory-threshold" must be less than "total-games"'

    logging.basicConfig(level=getattr(logging, args.log_level, None), format=LOG_FORMAT)
    
    neural_network = NNetWrapper(board_size=(args.board_size, args.board_size), batch_size=args.batch_size,
                                 epochs=args.epochs, lr=args.learning_rate, dropout=args.dropout)
    if args.weights_file:
        neural_network.load_checkpoint(args.weights_file)

    training(args.board_size, args.iterations, args.episodes, args.simulations, 
             args.constant_upper_confidence, args.temperature,  args.total_games, 
             args.victory_threshold, neural_network, args.temperature_threshold, 
             args.output_file, args.episode_threads, args.game_threads)

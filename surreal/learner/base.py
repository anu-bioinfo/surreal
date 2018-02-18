"""
Template class for all learners
"""
import surreal.utils as U
from surreal.session import (
    extend_config, PeriodicTracker, PeriodicTensorplex,
    BASE_ENV_CONFIG, BASE_SESSION_CONFIG, BASE_LEARNER_CONFIG
)
from surreal.session import StatsTensorplex, Loggerplex
from surreal.distributed import ZmqClient, ParameterPublisher, ZmqClientPool
import queue
from easydict import EasyDict

class PrefetchBatchQueue(object):
    """
    Pre-fetch a batch of exp from sampler on Replay side
    """
    def __init__(self,
                 sampler_host,
                 sampler_port,
                 batch_size,
                 max_size,):
        self._queue = queue.Queue(maxsize=max_size)
        self._batch_size = batch_size
        self._client = ZmqClientPool(
            host=sampler_host,
            port=sampler_port,
            request=self._batch_size,
            handler=self._enqueue,
            is_pyobj=True,
        )
        self._enqueue_thread = None

        self.timer = U.TimeRecorder()

    def _enqueue(self, item):
        self._queue.put(item, block=True)            

    def start_enqueue_thread(self):
        """
        Producer thread, runs sampler function on a priority replay structure
        Args:
            sampler: function batch_i -> list
                returns exp_dicts with 'obs_pointers' field
            start_sample_condition: function () -> bool
                begins sampling only when this returns True.
                Example: when the replay memory exceeds a threshold size
            start_sample_condvar: threading.Condition()
                notified by Replay.insert() when start sampling condition is met
            evict_lock: do not evict in the middle of fetching exp, otherwise
                we might fetch a null exp that just got evicted.
                locked by Replay.evict()
        """
        if self._enqueue_thread is not None:
            raise RuntimeError('Enqueue thread is already running')
        self._enqueue_thread = self._client
        self._client.start()
        return self._enqueue_thread

    def dequeue(self):
        """
        Called by the neural network, draw the next batch of experiences
        """
        with self.timer.time():
            return self._queue.get(block=True)

    def __len__(self):
        return self._queue.qsize()


learner_registry = {}

def register_learner(target_class):
    learner_registry[target_class.__name__] = target_class

def learnerFactory(learner_name):
    return learner_registry[learner_name]

class LearnerMeta(U.AutoInitializeMeta):
    def __new__(meta, name, bases, class_dict):
        cls = super().__new__(meta, name, bases, class_dict)
        register_learner(cls)
        return cls

class LearnerCore(metaclass=LearnerMeta):
    def __init__(self, *,
                 sampler_host,
                 sampler_port,
                 ps_publish_port,
                 batch_size,
                 max_prefetch_batch_queue):
        """
        Write log to self.log

        Args:
            sampler_host: client to connect to replay node sampler
            sampler_port: client to connect to replay node
            ps_pub_port: parameter server PUBLISH port
        """
        self._ps_publisher = None  # in _initialize()
        self._ps_port = ps_publish_port
        self._prefetch_queue = PrefetchBatchQueue(
            sampler_host=sampler_host,
            sampler_port=sampler_port,
            batch_size=batch_size,
            max_size=max_prefetch_batch_queue,
        )

        self.learn_timer = U.TimeRecorder()
        self.fetch_timer = self._prefetch_queue.timer
        self.iter_timer = U.TimeRecorder()

    def _initialize(self):
        """
        For AutoInitializeMeta interface
        """
        self._ps_publisher = ParameterPublisher(
            port=self._ps_port,
            module_dict=self.module_dict()
        )
        self._prefetch_queue.start_enqueue_thread()

    def default_config(self):
        """
        Returns:
            a dict of defaults.
        """
        return BASE_LEARNER_CONFIG

    def learn(self, batch_exp):
        """
        Abstract method runs one step of learning

        Args:
            batch_exp: batched experience, format is a list of whatever experience sender wrapper returns

        Returns:
            td_error or other values for prioritized replay
        """
        raise NotImplementedError

    def module_dict(self):
        """
        Dict of modules to be broadcasted to the parameter server.
        MUST be consistent with the agent's `module_dict()`
        """
        raise NotImplementedError

    def save(self, file_path):
        """
        Checkpoint to disk
        """
        raise NotImplementedError

    def publish_parameter(self, iteration, message=''):
        """
        Learner publishes latest parameters to the parameter server.

        Args:
            iteration: the current number of learning iterations
            message: optional message, must be pickleable.
        """
        self._ps_publisher.publish(iteration, message=message)

    def fetch_batch(self):
        return self._prefetch_queue.dequeue()

    def fetch_iterator(self):
        while True:
            yield self.fetch_batch()

    def main_loop(self):    
        """
            Main loop that defines learner process
        """
        for i, batch in enumerate(self.fetch_iterator()):
            with self.iter_timer.time():
                with self.learn_timer.time():
                    self.learn(batch)
                self.publish_parameter(i, message='batch '+str(i))


class Learner(LearnerCore):
    """
        Important: When extending this class, make sure to follow the init method signature so that 
        orchestrating functions can properly initialize the learner.
    """
    def __init__(self,
                 learner_config,
                 env_config,
                 session_config):
        """
        Write log to self.log

        Args:
            config: a dictionary of hyperparameters. It can include a special
                section "log": {logger configs}
            model: utils.pytorch.Module for the policy network
        """
        self.learner_config = extend_config(learner_config, self.default_config())
        self.env_config = extend_config(env_config, BASE_ENV_CONFIG)
        self.session_config = extend_config(session_config, BASE_SESSION_CONFIG)
        super().__init__(
            sampler_host=self.session_config.replay.sampler_host,
            sampler_port=self.session_config.replay.sampler_port,
            ps_publish_port=self.session_config.ps.publish_port,
            batch_size=self.learner_config.replay.batch_size,
            max_prefetch_batch_queue=self.session_config.replay.max_prefetch_batch_queue
        )
        self.log = Loggerplex(
            name='learner',
            session_config=self.session_config
        )
        self.tensorplex = StatsTensorplex(
            section_name='learner',
            session_config=self.session_config
        )
        self._periodic_tensorplex = PeriodicTensorplex(
            tensorplex=self.tensorplex,
            period=self.session_config.tensorplex.update_schedule.learner,
            is_average=True,
            keep_full_history=False
        )

    def default_config(self):
        """
        Returns:
            a dict of defaults.
        """
        return BASE_LEARNER_CONFIG

    def update_tensorplex(self, tag_value_dict, global_step=None):
        """
        Args:
            tag_value_dict:
            global_step: None to use internal tracker value
        """
        learn_time = self.learn_timer.avg + 1e-6
        fetch_time = self.fetch_timer.avg + 1e-6
        iter_time = self.iter_timer.avg + 1e-6
        # Time it takes to learn from a batch
        tag_value_dict['speed/learn_time'] = learn_time
        # Time it takes to fetch a batch
        tag_value_dict['speed/fetch_time'] = fetch_time
        # Time it takes to complete one full iteration
        tag_value_dict['speed/iter_time'] = iter_time
        # Percent of time spent on learning
        tag_value_dict['speed/compute_bound_percent'] = min(learn_time / iter_time * 100, 100)
        # Percent of time spent on IO
        tag_value_dict['speed/io_bound_percent'] = min(fetch_time / iter_time * 100, 100)
        self._periodic_tensorplex.update(tag_value_dict, global_step)

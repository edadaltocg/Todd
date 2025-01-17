from abc import ABC
from typing import TypeVar, Dict, List, Union

import torch
from transformers.modeling_outputs import ModelOutput


def mask_pad_tokens(
    sequences: torch.Tensor, scores: torch.Tensor, pad_token_id: int
) -> torch.Tensor:
    """
    Creates a mask for the padding tokens in a sequence of tokens.
    :param sequences: (*, seq_len) tensor of token ids
    :param pad_token_id: id of the padding token
    :return: (*, seq_len) tensor of 0s and 1s
    """

    # Todo: weird check
    # Sometime scores and sequences gen size are different and i have no idea why
    if sequences.shape[1] != scores.shape[1]:
        mask = sequences[:, :-1] != pad_token_id
    else:
        mask = sequences != pad_token_id

    return mask


def mean_score_remove_padding(
    sequences: torch.Tensor, scores: torch.Tensor, pad_token_id: int
) -> torch.Tensor:
    """
    Computes the mean score of a sequence of tokens, removing the padding tokens.
    :param sequences: (*, seq_len) tensor of token ids
    :param scores: (*, seq_len) tensor of scores
    :param pad_token_id: id of the padding token
    :return: (*,) tensor of mean scores
    """

    mask = mask_pad_tokens(sequences, scores, pad_token_id)

    return ((scores * mask.float()).sum(dim=-1) / mask.sum(dim=-1).float()).squeeze()


class Scorer(ABC):
    def __init__(self):
        # List of the scores computed by the scorer
        # List of keys returned in the compute_scores_benchmark method
        # Can be empty if the filter returns a single score
        self.score_names: List[str] = []

    def __call__(self, *args, **kwargs):
        return self.compute_scores(*args, **kwargs)

    def fit(self, *args, **kwargs):
        pass

    def accumulate(self, *args, **kwargs):
        pass

    def compute_scores(self, *args, **kwargs) -> torch.Tensor:
        """
        Should return an anomaly score: a higher score means more likely to be an anomaly.
        """
        raise NotImplementedError

    def compute_scores_benchmark(
        self, output: ModelOutput
    ) -> Union[Dict[str, torch.Tensor], torch.Tensor]:
        """
        Compute the scores of the output of the model.
        :param output: output of the model
        :return: dictionary of scores
        """
        raise NotImplementedError

    def __repr__(self):
        return self.__class__.__name__

    def __format__(self, format_spec):
        return self.__repr__()


ScorerType = TypeVar("ScorerType", bound=Scorer)


class EncoderBasedScorers(Scorer):
    def __init__(self):
        super().__init__()


class DecoderBasedScorers(Scorer):
    def __init__(self, mode: str = "input"):
        super().__init__()
        self.mode = mode

    def compute_scores(self, *args, **kwargs) -> torch.Tensor:
        try:
            if self.mode == "input":
                return self.per_input_scores(*args, **kwargs)
            elif self.mode == "output":
                return self.per_output_scores(*args, **kwargs)
            elif self.mode == "token":
                return self.per_token_scores(*args, **kwargs)
            else:
                raise ValueError(
                    f"Invalid mode {self.mode}. Should be one of ['input', 'output', 'token']"
                )
        except NotImplementedError as e:
            raise NotImplementedError(
                f"per_{self.mode}_scores is not implemented in {self.__class__.__name__}. "
                f"Maybe it is a bug or maybe you are using a filter that does not support this mode."
            ) from e

    def per_token_scores(self, *args, **kwargs) -> torch.Tensor:
        raise NotImplementedError

    def per_output_scores(self, *args, **kwargs) -> torch.Tensor:
        raise NotImplementedError

    def per_input_scores(self, *args, **kwargs) -> torch.Tensor:
        raise NotImplementedError


class LikelihoodScorer(DecoderBasedScorers):
    """
    Filters a batch of output based on the likelihood of the first sequence returned for each input.
    """

    def __init__(self, mode="input", num_return_sequences=1):
        super().__init__(mode=mode)
        self.num_return_sequences = num_return_sequences

    def per_output_scores(self, output: ModelOutput) -> torch.Tensor:
        sequences_scores = output.sequences_scores
        sequences_scores = sequences_scores.view(-1, self.num_return_sequences)

        return sequences_scores

    def per_input_scores(
        self, output: ModelOutput, num_return_sequences: int = 1
    ) -> torch.Tensor:
        # bs, num_return_sequences
        per_output_scores = self.per_output_scores(output)

        # todo: add option to change aggregation function
        # bs
        return per_output_scores[:, 0]

    def __format__(self, format_spec):
        return f"{self.__class__.__name__}(mode={self.mode})"


class SequenceSoftMaxScorerBase(DecoderBasedScorers):
    def __init__(
        self,
        temperature: float = 2.0,
        pad_token_id: int = 0,
        mode="input",
    ):
        super().__init__(mode=mode)
        self.pad_token_id = pad_token_id
        self.temperature = temperature

    def mk_probability(self, scores: torch.Tensor) -> torch.Tensor:
        return torch.softmax(scores / self.temperature, dim=-1)

    def aggregate_step_by_step_scores(
        self,
        sequences: torch.Tensor,
        per_step_scores: torch.Tensor,
        num_return_sequences: int,
    ) -> torch.Tensor:
        """
        :param sequences: (batch_size*numreturn, seq_len) tensor of token ids
        :param per_step_scores: (batch_size*numreturn, seq_len) tensor of scores
        :param num_return_sequences: number of sequences returned by the model
        :return: (batch_size, 1) tensor of aggregated scores
        """

        batch_size = sequences.shape[0] // num_return_sequences

        # (batch_size, num_seq_return, nun_gen_steps)
        per_step_scores = per_step_scores.squeeze(-1)
        per_step_scores = per_step_scores.view(batch_size * num_return_sequences, -1)

        # (batch_size*numbeam*numreturn, 1)
        anomaly_scores = mean_score_remove_padding(
            sequences, per_step_scores, self.pad_token_id
        )

        # (batch_size, numreturn)
        anomaly_scores = anomaly_scores.view(batch_size, num_return_sequences)

        return anomaly_scores


class SequenceMSPScorer(SequenceSoftMaxScorerBase):
    """
    Compute the Maximum Softmax Probability score of the input
    sequences and return a mask tensor with True for the sequences to keep.
    """

    def __init__(
        self,
        temperature: float = 2.0,
        pad_token_id: int = 0,
        mode="input",
    ):
        super().__init__(temperature, pad_token_id, mode=mode)

    def per_token_scores(
        self,
        output: ModelOutput,
        num_return_sequences: int = 1,
        num_beam: int = 1,
    ) -> torch.Tensor:
        """
        Returns OOD scores per generated token based on the probability distribution they have been generated from.
        @param output: ModelOutput object.
        @param num_return_sequences: number of sequences returned by the model.
        @param num_beam: number of beams used by the model
        @return: (batch_size, num_return_sequences, seq_len) tensor of scores.
        """

        batch_size = output.sequences.shape[0] // self.num_return_sequences
        sequences = output.sequences
        probabilities = self.mk_probability(output.scores)
        per_step_scores = torch.max(probabilities, dim=-1)

        return per_step_scores.view(batch_size, num_return_sequences, -1)

    def per_output_scores(
        self,
        output: ModelOutput,
        num_return_sequences: int = 1,
        num_beam: int = 1,
    ) -> torch.Tensor:
        sequences = output.sequences
        probabilities = self.mk_probability(output.scores)
        per_step_scores = torch.max(probabilities, dim=-1)

        return self.aggregate_step_by_step_scores(
            sequences, per_step_scores, num_return_sequences
        )

    def __format__(self, format_spec):
        return f"{self.__class__.__name__}(mode={self.mode}, temperature={self.temperature}, mode={self.mode})"

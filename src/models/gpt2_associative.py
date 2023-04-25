import torch
import torch.nn as nn
from transformers import GPT2LMHeadModel, GPT2Config
from memory.associative import AssociativeMemory
from typing import Optional, Any


class VardaGPTAssociative(nn.Module):
    def __init__(
        self,
        gpt2_model_name: str = "gpt2-small",
        memory_size: int = 10000,
        memory_dim: int = 768,
        index_type: str = "flat",
        num_clusters: int = 1024,
        num_search_results: int = 5,
        use_gpu: bool = False,
    ):
        super(VardaGPTAssociative, self).__init__()

        # Set up the device for the model
        self.device = torch.device("cuda" if use_gpu and torch.cuda.is_available() else "cpu")

        # Load the GPT-2 model and configuration
        self.gpt2_config = GPT2Config.from_pretrained(gpt2_model_name)
        self.gpt2_model = GPT2LMHeadModel.from_pretrained(gpt2_model_name)

        # Initialize the AssociativeMemory module
        self.memory = AssociativeMemory(
            memory_size=memory_size,
            embedding_dim=memory_dim,
            index_type=index_type,
            num_clusters=num_clusters,
            use_gpu=use_gpu,
        )

        # Define dimensions for search results and output
        self.search_results_dim = memory_dim * num_search_results

        # Linear layers for concatenated input, storable vector, store decision, delete decision, and deletable vector
        self.fc = nn.Linear(self.gpt2_config.n_embd + self.search_results_dim, self.gpt2_config.n_embd)
        self.fc_storable_vector = nn.Linear(self.gpt2_config.n_embd, memory_dim)
        self.fc_store_decision = nn.Linear(self.gpt2_config.n_embd, 1)
        self.fc_delete_decision = nn.Linear(self.gpt2_config.n_embd, num_search_results)
        self.fc_deletable_vector = nn.Linear(self.gpt2_config.n_embd, memory_dim)

        # Move all layers to the device
        self.to(self.device)

        self.num_search_results = num_search_results

    def forward(self, input_vectors: torch.Tensor, memory_input: Optional[torch.Tensor] = None) -> Any:
        input_vectors = input_vectors.to(self.device)

        # Search for relevant results if memory_input is provided
        if memory_input is not None:
            indices, distances = self.memory.search(memory_input.cpu().numpy())

            # Retrieve and concatenate search results with input vectors
            search_results = self.memory.get_all_embeddings()[indices].reshape(-1, self.search_results_dim)
            search_results = torch.tensor(search_results).to(self.device)
            concatenated_input = torch.cat([input_vectors, search_results], dim=-1)

            # Pass concatenated input through linear layer
            input_vectors = self.fc(concatenated_input)

        # Pass input_vectors through GPT-2 model's transformer and obtain hidden states
        transformer_outputs = self.gpt2_model.transformer(inputs_embeds=input_vectors)
        hidden_states = transformer_outputs.last_hidden_state

        # Get logits from hidden states
        logits = self.gpt2_model.lm_head(hidden_states)

        # Calculate storable vector, store decision, delete decision, and deletable vector
        storable_vector = self.fc_storable_vector(hidden_states)
        store_decision = self.fc_store_decision(hidden_states)
        delete_decision = self.fc_delete_decision(hidden_states)
        deletable_vector = self.fc_deletable_vector(hidden_states)

        # Store the storable_vector in the associative memory if the store_decision is affirmative
        store_threshold = 0.5  # Define a threshold for store decision
        store_mask = (store_decision > store_threshold).float()
        storable_vector_to_store = storable_vector * store_mask
        self.memory.add(storable_vector_to_store)

        # Calculate the L2 distances between deletable_vector and search_results
        expanded_deletable_vector = deletable_vector.unsqueeze(1).expand(-1, self.num_search_results, -1)
        expanded_search_results = search_results.unsqueeze(0).expand(input_vectors.size(0), -1, -1)
        squared_distances = torch.sum((expanded_deletable_vector - expanded_search_results) ** 2, dim=-1)
        l2_distances = torch.sqrt(squared_distances)

        # Remove embeddings from the memory if the L2 distance is above a threshold
        threshold = 0.5
        indices_to_delete = torch.nonzero(l2_distances > threshold, as_tuple=True)
        indices_to_delete_flat = indices_to_delete[0].view(-1)
        self.memory.remove(indices_to_delete_flat.cpu().numpy())

        return logits

from collections import OrderedDict
import math 
import torch
import copy
import sys
import numpy as np
from utils import one_hot_encode, capsnet_testing_loss
from torch.autograd import Variable
from torch.backends import cudnn
from quantization_methods import *
from quantized_models import *


def quantized_test(model, num_classes, data_loader, quantization_function, scaling_factors, quantization_bits,
                   quantization_bits_routing):
    """ Function to test the accuracy of the quantized models

        Args:
            model: pytorch model
            num_classes: number ot classes of the dataset
            data_loader: data loader of the test dataset
            quantization_function: quantization function of the quantization method to use
            quantization_bits: list, quantization bits for the activations
            quantization_bits_routing: list, quantization bits for the dynamic routing
        Returns:
            accuracy_percentage: accuracy of the quantized model expressed in percentage """
    # Switch to evaluate mode
    model.eval()

    loss = 0
    correct = 0

    num_batches = len(data_loader)

    for data, target in data_loader:
        batch_size = data.size(0)
        target_one_hot = one_hot_encode(target, length=num_classes)

        if torch.cuda.device_count() > 0:  # if there are available GPUs, move data to the first visible
            device = torch.device("cuda:0")
            data = data.to(device)
            target = target.to(device)
            target_one_hot = target_one_hot.to(device)

        # input quantization 
        data = quantization_function(data, scaling_factors[0], quantization_bits[0])

        # Output predictions
        output = model(data, quantization_function, scaling_factors[1:], quantization_bits, quantization_bits_routing)

        # Sum up batch loss
        m_loss = \
            capsnet_testing_loss(output, target_one_hot)
        loss += m_loss.data

        # Count number of correct predictions
        # Compute the norm of the vector capsules
        v_length = torch.sqrt((output ** 2).sum(dim=2))
        assert v_length.size() == torch.Size([batch_size, num_classes])

        # Find the index of the longest vector
        _, max_index = v_length.max(dim=1)
        assert max_index.size() == torch.Size([batch_size])

        # vector with 1 where the model makes a correct prediction, 0 where false
        correct_pred = torch.eq(target.cpu(), max_index.data.cpu())
        correct += correct_pred.sum()

    # Log test accuracies
    num_test_data = len(data_loader.dataset)
    accuracy_percentage = float(correct) * 100.0 / float(num_test_data)

    return accuracy_percentage


def qcapsnets(model, model_parameters, full_precision_filename, num_classes, data_loader, top_accuracy,
              accuracy_tolerance, memory_budget, quantization_scheme, std_multiplier=100):
    """ Q-CapsNets framework - Quantization

        Args:
            model: string, name of the model
            model_parameters: list, parameters to use for the instantiation of the model class
            full_precision_filename: string, directory of the full-precision weights
            num_classes: number of classes of the dataset
            data_loader: data loader of the testing dataset
            top_accuracy : maximum accuracy reached by the full_precision trained model (percentage)
            accuracy_tolerance: tolerance of the quantized model accuracy with respect to the full precision accuracy.
                                Provided in percentage
            memory_budget: memory budget for the weights of the model. Provided in MB (MegaBytes)
            quantization_scheme: quantization scheme to be used by the framework (string, e.g., "truncation)"
        Returns:
            void
    """
    print("==> Q-CapsNets Framework")
    # instantiate the quantized model with the full-precision weights
    model_quant_class = getattr(sys.modules[__name__], model)
    model_quant_original = model_quant_class(*model_parameters)
    model_quant_original.load_state_dict(torch.load(full_precision_filename))
    
    weights_scale_factors_original = torch.load(full_precision_filename[:-3]+'_w_info.pt', map_location=torch.device('cpu'))
    weights_scale_factors = OrderedDict()
    for key, value in weights_scale_factors_original.items(): 
        weights_scale_factors[key] = torch.min(value[0], value[1]*std_multiplier)
        
        
    # from sqnr, generate sorted list with name of weights 
    # tuple name - sqnr 
    sqnr_tuples = [] 
    i = 0 
    for key, value in weights_scale_factors_original.items(): 
        if "weight" in key and "batchnorm" not in key:
            sqnr_tuples.append((key, value[2].item(), i))
            i += 1 
    # sort list of tuples based on second element (sqnr) 
    sqnr_tuples.sort(key=lambda tup: tup[1], reverse=True)
    print(sqnr_tuples)
    
        
    # Load scaling factors 
    act_info = torch.load(full_precision_filename[:-3]+'_a_info.pt', map_location=torch.device('cpu'))
    act_scale_factors = act_info["scaling_factors"].tolist()
    act_sqnr = [] 
    for i, (key, value) in enumerate(act_info["sqnr"].items()): 
        act_sqnr.append((key, value, i))
    act_sqnr.sort(key=lambda tup: tup[1], reverse=False)
    if len(act_sqnr) > 4: 
        num_layer_per_sqnr_group = math.floor(len(act_sqnr) / 4)
        act_sqnr_grouped_index = [[act_sqnr[i][2] for i in range(0, num_layer_per_sqnr_group)]]
        act_sqnr_grouped_index.append([act_sqnr[i][2] for i in range(num_layer_per_sqnr_group, 2*num_layer_per_sqnr_group)])
        act_sqnr_grouped_index.append([act_sqnr[i][2] for i in range(2*num_layer_per_sqnr_group, 3*num_layer_per_sqnr_group)])
        act_sqnr_grouped_index.append([act_sqnr[i][2] for i in range(3*num_layer_per_sqnr_group, len(act_sqnr))])
    else: 
        act_sqnr_grouped_index = []
        for i in range(len(act_sqnr)): 
            act_sqnr_grouped_index.append([act_sqnr[i][2]])
    
    print(f'DEBUG act_sqnr {act_sqnr}')        
    print(f'DEBUG act_sqnr_grouped {act_sqnr_grouped_index}')
        

    # Move the model to GPU if available
    if torch.cuda.device_count() > 0:
        device = torch.device("cuda:0")
        model_quant_original.to(device)
        cudnn.benchmark = True

    # create the quantization functions
    possible_functions = globals().copy()
    possible_functions.update(locals())
    quantization_function_activations = possible_functions.get(quantization_scheme)
    if not quantization_function_activations:
        raise NotImplementedError("Quantization function %s not implemented" % quantization_scheme)
    quantization_function_weights = possible_functions.get(quantization_scheme + "_inplace")
    if not quantization_function_weights:
        raise NotImplementedError("Quantization function %s not implemented (inplace version)" % quantization_scheme)

    # compute the accuracy reduction available for each step
    minimum_accuracy = top_accuracy - accuracy_tolerance / 100 * top_accuracy
    acc_reduction = top_accuracy - minimum_accuracy
    step1_reduction = 5 / 100 * acc_reduction
    step1_min_acc = top_accuracy - step1_reduction

    print(f"Full-precision accuracy: {top_accuracy:.2f} %")
    print(f"Minimum quantized accuracy: {minimum_accuracy:.2f} %")
    print(f"Memory budget: {memory_budget:.2f} MB")
    print(f"Quantization method: {quantization_scheme}")
    
    
    tot_numer_of_weights = 0 
    for i, c in enumerate(model_quant_original.named_children()):
        for p in c[1].named_parameters():
            tot_numer_of_weights += p[1].numel()
            
    tot_memory_b = tot_numer_of_weights * 32 
    tot_memory_B = tot_memory_b // 8 
    tot_memory_MB = tot_memory_B / 2**20
    
    print(f"Baseline memory footprint (MB): {tot_memory_MB:.2f} MB")
    
    
    # function to get all the leaf modules of the network
    # leaf_children = []
    # leaf_children_names = []
    def get_leaf_modules(top_name, m, leaf_children, leaf_children_names): 
        # m: generator 
        for key, value in m: 
            if hasattr(value, "leaf") and value.leaf == True: 
                leaf_children.append(value)
                leaf_children_names.append(top_name + '.' + key)
                # print(top_name + '.' + key)
            else: 
                leaf_children, leaf_children_names = get_leaf_modules(top_name + '.' + key, value.named_children(), leaf_children, leaf_children_names)
        return leaf_children, leaf_children_names
    
    # STEP 1: Layer-Uniform quantization of weights and activations
    print("STEP 1")

    def step1_quantization_test(quantization_bits):
        """ Function to test the model at STEP 1 of the algorithm

            The function receives a single "quantization_bits" value N, and creates two lists [N, N, ..., N] and
            [N, N, ..., N] for the activations and the dynamic routing, since at STEP 1 all the layers are quantized
            uniformly. The weights of each layer are quantized with N bits too and then the accuracy of the model
            is computed.

            Args:
                quantization_bits: single value used for quantizing all the weights and activations
            Returns:
                acc_temp: accuracy of the model quantized uniformly with quantization_bits bits
        """
        quantized_model_temp = copy.deepcopy(model_quant_original)

        step1_act_bits_f = []     # list with the quantization bits for the activations
        step1_dr_bits_f = []      # list with the quantization bits for the dynamic routing
        
        #### QUANTIZE THE WEIGHTS #####
        #leaf_children = [] 
        #leaf_children_names = []
        leaf_children, leaf_children_names = get_leaf_modules("", quantized_model_temp.named_children(), [], [])
        for i, (name, children) in enumerate(zip(leaf_children_names, leaf_children)): 
            for p in children.named_parameters(): 
                if "batchnorm" not in p[0]:
                    with torch.no_grad(): 
                        quantization_function_weights(p[1], weights_scale_factors['.'.join([name[1:],p[0]])].item(), quantization_bits)
        #################################
            step1_act_bits_f.append(quantization_bits)
            if children.capsule_layer and children.dynamic_routing: 
                step1_dr_bits_f.append(quantization_bits)

        # test with quantized weights and activations
        acc_temp = quantized_test(quantized_model_temp, num_classes, data_loader,
                                  quantization_function_activations, act_scale_factors, step1_act_bits_f, step1_dr_bits_f)
        print(quantization_bits, step1_act_bits_f, step1_dr_bits_f, acc_temp)
        del quantized_model_temp
        return acc_temp

    # BINARY SEARCH of the bitwidth for step 1, starting from 32 bits
    step1_bit_search = [32]
    step1_acc_list = []      # list of accuracy at each step of the search algorithm
    step1_acc = step1_quantization_test(32)
    step1_acc_list.append(step1_acc)
    if step1_acc > step1_min_acc:
        step1_bit_search_sat = [True]    # True is the accuracy is higher than the minimum required
        step1_bit_search.append(16)
        while True:
            step1_acc = step1_quantization_test(step1_bit_search[-1])
            step1_acc_list.append(step1_acc)
            if step1_acc > step1_min_acc:
                step1_bit_search_sat.append(True)
            else:
                step1_bit_search_sat.append(False)
            if (abs(step1_bit_search[-1] - step1_bit_search[-2])) == 1:
                step1_bit_search_sat.reverse()
                step1_bits = step1_bit_search[
                    len(step1_bit_search_sat) - 1 - next(k for k, val in enumerate(step1_bit_search_sat) if val)]
                step1_bit_search_sat.reverse()
                step1_acc = step1_acc_list[
                    len(step1_bit_search_sat) - 1 - next(k for k, val in enumerate(step1_bit_search_sat) if val)]
                break
            else:
                if step1_acc > step1_min_acc:
                    step1_bit_search.append(
                        int(step1_bit_search[-1] - abs(step1_bit_search[-1] - step1_bit_search[-2]) / 2))
                else:
                    step1_bit_search.append(
                        int(step1_bit_search[-1] + abs(step1_bit_search[-1] - step1_bit_search[-2]) / 2))
    else:
        step1_bits = 32
        step1_acc = step1_acc_list[0]

    # Create the lists of bits of STEP 1
    step1_act_bits = []
    step1_dr_bits = []
    step1_weight_bits = []         
    #leaf_children = [] 
    #leaf_children_names = []
    leaf_children, leaf_children_names = get_leaf_modules("", model_quant_original.named_children(), [], [])
    for i, (name, children) in enumerate(zip(leaf_children_names, leaf_children)): 
        step1_act_bits.append(step1_bits)
        step1_weight_bits.append(step1_bits)
        if children.capsule_layer and children.dynamic_routing: 
            step1_dr_bits.append(step1_bits)

    print("STEP 1 output: ")
    print("\t Weight bits: \t\t", step1_weight_bits)
    print("\t Activation bits: \t\t", step1_act_bits)
    print("\t Dynamic Routing bits: \t\t", step1_dr_bits)
    print("STEP 1 accuracy: ", step1_acc)
    print("\n")

    # STEP2 - satisfy memory requirement
    # compute the number of weights and biases of each layer/block
    print("STEP 2")
    number_of_weights_inlayers = []
    for c in leaf_children:
        param_intra_layer = 0
        for p in c.parameters():
            param_intra_layer = param_intra_layer + p.numel()
        number_of_weights_inlayers.append(param_intra_layer)
    number_of_blocks = len(number_of_weights_inlayers)

    memory_budget_bits = memory_budget * 8 * 2**20      # From MB to bits
    minimum_mem_required = np.sum(number_of_weights_inlayers)

    if memory_budget_bits < minimum_mem_required:
        #raise ValueError("The memory budget can not be satisfied, increase it to",
        #                 minimum_mem_required / 8 / 2**20, " MB at least")
        return f"ERROR The memory budget can not be satisfied, increase it to {minimum_mem_required / 8 / 2**20} MB at least"

    # Compute the number of bits that satisfy the memory budget.
    # Uniform [N, N, N, ..., N]
    step2_weight_bits = math.floor(memory_budget_bits / minimum_mem_required)
    step2_weight_bits = [step2_weight_bits for _ in range(len(step1_weight_bits))]

    # lists of bitwidths for activations and dynamic routing at STEP 1
    step2_act_bits = copy.deepcopy(step1_act_bits)
    step2_dr_bits = copy.deepcopy(step1_dr_bits)

    # Quantized the weights
    model_memory = copy.deepcopy(model_quant_original)
    #leaf_children = [] 
    #leaf_children_names = []
    leaf_children, leaf_children_names = get_leaf_modules("", model_memory.named_children(), [], [])
    for i, (name, children) in enumerate(zip(leaf_children_names, leaf_children)): 
        for p in children.named_parameters(): 
            if "batchnorm" not in p[0]:
                with torch.no_grad(): 
                    quantization_function_weights(p[1], weights_scale_factors['.'.join([name[1:],p[0]])].item(), step2_weight_bits[i])

    step2_acc = quantized_test(model_memory, num_classes, data_loader,
                               quantization_function_activations, act_scale_factors, step2_act_bits, step2_dr_bits)
    print(step2_weight_bits, step2_act_bits, step2_dr_bits, step2_acc)
    
    model_memory_acc = step2_acc 
    model_memory_weight_bits = copy.deepcopy(step2_weight_bits)
    model_memory_act_bits = copy.deepcopy(step2_act_bits)
    model_memory_dr_bits = copy.deepcopy(step2_dr_bits)
    
    if step2_acc >= minimum_accuracy: 
        branchB = False 
    else: 
        branchB = True 
        # raise the weights until I satisfy the accuracy 
        while step2_acc < minimum_accuracy: 
            step2_weight_bits = [step2_weight_bits[i]+1 for i in range(len(step2_weight_bits))]
            model_step2 = copy.deepcopy(model_quant_original)
            #leaf_children = [] 
            #leaf_children_names = []
            leaf_children, leaf_children_names = get_leaf_modules("", model_step2.named_children(), [], [])
            for i, (name, children) in enumerate(zip(leaf_children_names, leaf_children)): 
                for p in children.named_parameters(): 
                    if "batchnorm" not in p[0]:
                        with torch.no_grad(): 
                            quantization_function_weights(p[1], weights_scale_factors['.'.join([name[1:],p[0]])].item(), step2_weight_bits[i])

            step2_acc = quantized_test(model_step2, num_classes, data_loader,
                                    quantization_function_activations, act_scale_factors, step2_act_bits, step2_dr_bits)
            print(step2_weight_bits, step2_act_bits, step2_dr_bits, step2_acc)
            
        # for l in sorted_layers
        for l in sqnr_tuples: 
            # while acc > min_acc
            while  step2_acc > minimum_accuracy and step2_weight_bits[l[2]] > 1: 
                # BWl -= 1 
                step2_weight_bits[l[2]] -= 1
                model_step2 = copy.deepcopy(model_quant_original)
                #leaf_children = [] 
                #leaf_children_names = []
                leaf_children, leaf_children_names = get_leaf_modules("", model_step2.named_children(), [], [])
                for i, (name, children) in enumerate(zip(leaf_children_names, leaf_children)): 
                    for p in children.named_parameters(): 
                        if "batchnorm" not in p[0]:
                            with torch.no_grad(): 
                                #print(f"Quantize {p[0]} with {step2_weight_bits[i]}")
                                quantization_function_weights(p[1], weights_scale_factors['.'.join([name[1:],p[0]])].item(), step2_weight_bits[i])
                prev_step2_acc = step2_acc
                step2_acc = quantized_test(model_step2, num_classes, data_loader,
                                        quantization_function_activations, act_scale_factors, step2_act_bits, step2_dr_bits) 
                print(step2_weight_bits, step2_act_bits, step2_dr_bits, step2_acc)
            
            step2_weight_bits[l[2]] += 1 
            step2_acc = prev_step2_acc
            
            # break condition if I managed to satisfy the memory budget 
            # compute new memory occupation 
            curr_mem = sum([step2_weight_bits[i]*number_of_weights_inlayers[i] for i in range(len(step2_weight_bits))])
            print(f"curr_mem {curr_mem}, memory_budget_bits {memory_budget_bits}")
            if curr_mem <= memory_budget_bits: 
                branchB = False
                break 
                
    
    if branchB:   # was not able to satisfy both constraits 
        # MODEL MEMORY
        quantized_filename = full_precision_filename[:-3] + '_quantized_memory.pt'
        #torch.save(model_memory.state_dict(), quantized_filename)
        print("Model-memory stored in ", quantized_filename)
        print("\t Weight bits: \t\t", model_memory_weight_bits)
        print("\t Activation bits: \t\t", model_memory_act_bits)
        print("\t Dynamic Routing bits: \t\t", model_memory_dr_bits)
        print("Model_memory accuracy: ", model_memory_acc)
        assert len(number_of_weights_inlayers) == len(step2_weight_bits)
        final_weight_memory_b = sum([number_of_weights_inlayers[i]*model_memory_weight_bits[i] for i in range(len(number_of_weights_inlayers))])
        wmem_reduction_mem = tot_memory_b / final_weight_memory_b
        print(f"Model_memory weight mem reduction: {wmem_reduction_mem:.2f}x")
        print("\n")
        # MODEL ACCURACY 
        model_step2 = copy.deepcopy(model_quant_original)
        #leaf_children = [] 
        #leaf_children_names = []
        leaf_children, leaf_children_names = get_leaf_modules("", model_step2.named_children(), [], [])
        for i, (name, children) in enumerate(zip(leaf_children_names, leaf_children)): 
            for p in children.named_parameters(): 
                if "batchnorm" not in p[0]:
                    with torch.no_grad(): 
                        quantization_function_weights(p[1], weights_scale_factors['.'.join([name[1:],p[0]])].item(), step2_weight_bits[i])

        model_accuracy_acc = quantized_test(model_step2, num_classes, data_loader,
                                quantization_function_activations, act_scale_factors, step2_act_bits, step2_dr_bits)
        quantized_filename = full_precision_filename[:-3] + '_quantized_accuracy.pt'
        #torch.save(model_step2.state_dict(), quantized_filename)
        print("Model-accuracy stored in ", quantized_filename)
        print("\t Weight bits: \t\t", step2_weight_bits)
        print("\t Activation bits: \t\t", step2_act_bits)
        print("\t Dynamic Routing bits: \t\t", step2_dr_bits)
        print("Model_accuracy accuracy: ", model_accuracy_acc)
        assert len(number_of_weights_inlayers) == len(step2_weight_bits)
        final_weight_memory_b = sum([number_of_weights_inlayers[i]*step2_weight_bits[i] for i in range(len(number_of_weights_inlayers))])
        wmem_reduction_acc = tot_memory_b / final_weight_memory_b
        print(f"Model_accuracy weight mem reduction: {wmem_reduction_acc:.2f}x")
        
        return model_memory_weight_bits, model_memory_act_bits, model_memory_dr_bits, model_memory_acc, wmem_reduction_mem, step2_weight_bits, step2_act_bits, step2_dr_bits, step2_acc, wmem_reduction_acc

    print("STEP 2 output: ")
    print("\t Weight bits: \t\t", step2_weight_bits)
    print("\t Activation bits: \t\t", step2_act_bits)
    print("\t Dynamic Routing bits: \t\t", step2_dr_bits)
    print("STEP 2 accuracy: ", step2_acc)
    print("\n")
    
    del model_memory


    # What is the accuracy that can still be consumed?
    branchA_accuracy_budget = step2_acc - minimum_accuracy
    step3a_accuracy_budget = branchA_accuracy_budget * 55 / 100
    step3a_accuracy_budget_per_step = step3a_accuracy_budget / len(act_sqnr_grouped_index)
    step3A_min_acc = step2_acc 

    # STEP 3A  - layer-wise quantization of activations
    print("STEP 3A")
    # get the position of the layers that use dynamic routing bits
    dynamic_routing_bits_bool = []
    for c in leaf_children:
        if c.capsule_layer and c.dynamic_routing:
                dynamic_routing_bits_bool.append(True)
        else:
            dynamic_routing_bits_bool.append(False)
    layers_dr_position = [pos for pos, val in enumerate(dynamic_routing_bits_bool) if val]

    step3a_weight_bits = copy.deepcopy(step2_weight_bits)
    step3a_act_bits = copy.deepcopy(step2_act_bits)
    step3a_dr_bits = copy.deepcopy(step2_dr_bits)
    
    model_step2 = copy.deepcopy(model_quant_original)
    #leaf_children = [] 
    #leaf_children_names = []
    leaf_children, leaf_children_names = get_leaf_modules("", model_step2.named_children(), [], [])
    for i, (name, children) in enumerate(zip(leaf_children_names, leaf_children)): 
        for p in children.named_parameters(): 
            if "batchnorm" not in p[0]:
                with torch.no_grad(): 
                    quantization_function_weights(p[1], weights_scale_factors['.'.join([name[1:],p[0]])].item(), step3a_weight_bits[i])
    
    step3a_acc = step2_acc  
    
    start_bits = step3a_act_bits[0]
    for l in range(0, len(act_sqnr_grouped_index)):
        curr_bits = start_bits
        step3A_min_acc -= step3a_accuracy_budget_per_step
        while True:
            if step3a_acc >= step3A_min_acc:
                if curr_bits < 3: 
                    break 
                for i in range(len(act_sqnr_grouped_index[l])): 
                    step3a_act_bits[act_sqnr_grouped_index[l][i]] -= 1 
                    curr_bits = step3a_act_bits[act_sqnr_grouped_index[l][i]]
                step3a_acc_prev = step3a_acc
                step3a_acc = quantized_test(model_step2, num_classes, data_loader,
                                            quantization_function_activations, act_scale_factors, step3a_act_bits, step3a_dr_bits)
                print(step3a_weight_bits, step3a_act_bits, step3a_dr_bits, step3a_acc)
            else:
                for i in range(len(act_sqnr_grouped_index[l])): 
                    step3a_act_bits[act_sqnr_grouped_index[l][i]] += 1 
                step3a_acc = step3a_acc_prev
                break

    #step3a_acc = quantized_test(model_step2, num_classes, data_loader,
    #                            quantization_function_activations, act_scale_factors, step3a_act_bits, step3a_dr_bits)
    #print(step3a_weight_bits, step3a_act_bits, step3a_dr_bits, step3a_acc)

    print("STEP 3A output: ")
    print("\t Weight bits: \t\t", step3a_weight_bits)
    print("\t Activation bits: \t\t", step3a_act_bits)
    print("\t Dynamic Routing bits: \t\t", step3a_dr_bits)
    print("STEP 3A accuracy: ", step3a_acc)
    print("\n")

    # STEP 4A  -  layer-wise quantization of dynamic routing
    print("STEP 4A")
    step4a_weight_bits = copy.deepcopy(step2_weight_bits)
    step4a_act_bits = copy.deepcopy(step3a_act_bits)
    step4a_dr_bits = copy.deepcopy(step3a_dr_bits)

    # need to variate only the bits of the layers in which the dynamic routing is actually performed
    # (iterations > 1)
    dynamic_routing_quantization = []
    for c in leaf_children:
        if c.capsule_layer and c.dynamic_routing:
                dynamic_routing_quantization.append(True)
    dr_quantization_pos = [pos for pos, val in enumerate(dynamic_routing_quantization) if val]

    # new set of bits only if dynamic routing is performed
    dr_quantization_bits = [step4a_dr_bits[x] for x in dr_quantization_pos]
    
    step4a_acc = step3a_acc
    step4a_accuracy_budget = step4a_acc - minimum_accuracy
    step4a_accuracy_budget_per_step = step4a_accuracy_budget / len(dr_quantization_bits)
    step4a_min_acc = step4a_acc
    for l in range(0, len(dr_quantization_bits)):
        step4a_min_acc -= step4a_accuracy_budget_per_step
        while True:
            if step4a_acc >= step4a_min_acc:
                if dr_quantization_bits[l] < 3: 
                    break
                dr_quantization_bits[l] -= 1 
                # update the whole vector step4a_dr_bits
                for x in range(0, len(dr_quantization_bits)):
                    step4a_dr_bits[dr_quantization_pos[x]] = dr_quantization_bits[x]
                step4a_acc_prev = step4a_acc
                step4a_acc = quantized_test(model_step2, num_classes, data_loader,
                                            quantization_function_activations, act_scale_factors, step4a_act_bits, step4a_dr_bits)
                print(step4a_weight_bits, step4a_act_bits, step4a_dr_bits, step4a_acc)
            
            else:
                dr_quantization_bits[l] += 1 
                # update the whole vector step4a_dr_bits
                for x in range(0, len(dr_quantization_bits)):
                    step4a_dr_bits[dr_quantization_pos[x]] = dr_quantization_bits[x]
                    step4a_acc = step4a_acc_prev
                break

    #step4a_acc = quantized_test(model_step2, num_classes, data_loader,
    #                            quantization_function_activations, act_scale_factors, step4a_act_bits, step4a_dr_bits)
    #print(step4a_weight_bits, step4a_act_bits, step4a_dr_bits, step4a_acc)

    print("STEP 4A output: ")
    print("\t Weight bits: \t\t", step4a_weight_bits)
    print("\t Activation bits: \t\t", step4a_act_bits)
    print("\t Dynamic Routing bits: \t\t", step4a_dr_bits)
    print("STEP 4A accuracy: ", step4a_acc)
    print("\n")

    print("\n")
    quantized_filename = full_precision_filename[:-3] + '_quantized_satisfied.pt'
    #torch.save(model_step2.state_dict(), quantized_filename)
    print("Model-satisfied stored in ", quantized_filename)
    print("\t Weight bits: \t\t", step4a_weight_bits)
    print("\t Activation bits: \t\t", step4a_act_bits)
    print("\t Dynamic Routing bits: \t\t", step4a_dr_bits)
    print("Model-satisfied accuracy: ", step4a_acc)
    
    assert len(number_of_weights_inlayers) == len(step4a_weight_bits)
    final_weight_memory_b = sum([number_of_weights_inlayers[i]*step4a_weight_bits[i] for i in range(len(number_of_weights_inlayers))])
    final_weight_memory_B = final_weight_memory_b / 8 
    wmem_reduction = tot_memory_b / final_weight_memory_b
    print(f"Weight memory reduction: {wmem_reduction:.2f}x")
    
    return step4a_weight_bits, step4a_act_bits, step4a_dr_bits, step4a_acc, wmem_reduction


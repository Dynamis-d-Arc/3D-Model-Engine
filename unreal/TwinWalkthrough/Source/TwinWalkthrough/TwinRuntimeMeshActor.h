#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "ProceduralMeshComponent.h"
#include "TwinRuntimeMeshActor.generated.h"

/** One glTF node of a runtime-loaded building (architecture object, furniture item or HVAC element). */
UCLASS(NotPlaceable)
class TWINWALKTHROUGH_API ATwinRuntimeMeshActor : public AActor
{
	GENERATED_BODY()

public:
	ATwinRuntimeMeshActor()
	{
		PrimaryActorTick.bCanEverTick = false;
		Mesh = CreateDefaultSubobject<UProceduralMeshComponent>(TEXT("Mesh"));
		Mesh->bUseAsyncCooking = false;  // collision must exist before the nav mesh and the validation run
		Mesh->SetMobility(EComponentMobility::Static);
		// registered by ATwinViewerDirector once its sections exist: an empty procedural mesh registered at
		// spawn would enter the navigation octree with empty bounds and never be walkable
		Mesh->bAutoRegister = false;
		RootComponent = Mesh;
	}

	UPROPERTY(VisibleAnywhere, Category = "Twin")
	TObjectPtr<UProceduralMeshComponent> Mesh;
};
